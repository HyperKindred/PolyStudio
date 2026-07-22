"""聊天相关的 HTTP 路由。

本模块处于 FastAPI 的“接口层”，主要负责：
1. 接收并校验前端请求；
2. 将业务处理交给 history_service 和 agent_service；
3. 把 Agent 输出包装为 SSE 流；
4. 将同一批事件通过 WebSocket 广播给订阅画布的其他客户端。

真正的 Agent 编排和模型调用不在这里实现，入口是
``process_chat_stream``；画布持久化则由 ``history_service`` 负责。
"""
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import json
import os
import uuid
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from app.services.agent_service import process_chat_stream
from app.services.history_service import history_service
from app.services.connection_manager import manager

logger = logging.getLogger(__name__)

# APIRouter 用来集中声明本模块的路由；最终会在 app/main.py 中统一挂载到 /api。
router = APIRouter()


class ChatRequest(BaseModel):
    """POST /chat 的请求体结构，由 Pydantic 自动解析和校验。"""

    # 当前这一轮新输入的用户消息。
    message: str
    # 当前消息之前的上下文；前端使用 OpenAI 风格的 role/content 字典传递。
    messages: Optional[List[Dict[str, Any]]] = []
    # 可选的会话标识，继续向下传递给流处理器，用于区分日志或会话。
    session_id: Optional[str] = None
    # 当前画布标识。存在时，事件只广播给订阅该画布的 WebSocket 客户端。
    canvas_id: Optional[str] = None


@router.get("/canvases")
async def get_canvases():
    """读取全部项目（画布、消息和 Excalidraw 数据）。

    history_service 当前使用本地 JSON 文件持久化，因此这里不需要数据库查询代码。
    """
    return history_service.get_canvases()


@router.post("/canvases")
async def save_canvas(request: Request):
    """保存或更新画布（项目）

    注意：前端会携带 Excalidraw 的 data(elements/appState/files)，
    用 Pydantic 模型解析容易因 extra 字段处理/热重载不同步而丢字段。
    这里直接存原始 JSON，避免 data 被过滤导致刷新后画布空白。
    """
    # Request.json() 保留前端传来的完整嵌套结构，不经过额外模型字段过滤。
    payload = await request.json()
    return history_service.save_canvas(payload)


@router.delete("/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str):
    """按 URL 路径中的 canvas_id 删除一个项目。"""
    history_service.delete_canvas(canvas_id)
    # 返回固定结构，方便前端判断接口已成功执行。
    return {"success": True}


@router.post("/upload-image")
async def upload_image(file: UploadFile = File(...)):
    """
    上传图片到 storage/images 目录
    
    Returns:
        上传后的图片URL（相对路径，如 /storage/images/xxx.jpg）
    """
    try:
        # chat.py 位于 backend/app/routers，向上三级得到 backend 目录。
        BASE_DIR = Path(__file__).parent.parent.parent
        IMAGES_DIR = BASE_DIR / "storage" / "images"
        # parents=True 会一并创建缺失的父目录；exist_ok=True 允许目录已存在。
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        
        # 浏览器通常会为图片提供 image/png、image/jpeg 等 MIME 类型。
        content_type = file.content_type or ""
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="只支持图片文件")
        
        # 时间戳便于人工识别，短 UUID 用于避免同一秒上传时发生重名。
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        
        # 保留原扩展名；客户端没有提供文件名或扩展名时退化为 jpg。
        original_filename = file.filename or "image"
        ext = os.path.splitext(original_filename)[1] or ".jpg"
        if not ext.startswith("."):
            ext = ".jpg"
        
        filename = f"upload_{timestamp}_{unique_id}{ext}"
        file_path = IMAGES_DIR / filename
        
        # UploadFile.read() 是异步读取；本地 open/write 则把内容写入最终路径。
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # main.py 已把 backend/storage 挂载为 /storage，因此浏览器可直接访问此 URL。
        image_url = f"/storage/images/{filename}"
        return {"url": image_url, "filename": filename}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@router.post("/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    """
    上传音频到 storage/audios 目录
    支持的格式：mp3, wav, m4a, aac, ogg, flac, wma
    
    Returns:
        上传后的音频URL（相对路径，如 /storage/audios/xxx.mp3 或 /storage/audios/xxx.wav）
    """
    try:
        # 每个媒体类型使用独立目录，方便静态访问和后续工具处理。
        BASE_DIR = Path(__file__).parent.parent.parent
        AUDIOS_DIR = BASE_DIR / "storage" / "audios"
        AUDIOS_DIR.mkdir(parents=True, exist_ok=True)
        
        # MIME 类型来自客户端，可能缺失或不准确，所以后面还会检查扩展名。
        content_type = file.content_type or ""
        # 支持的音频 MIME 类型
        allowed_audio_types = [
            "audio/",  # 通用音频类型（audio/mpeg, audio/wav, audio/mp4 等）
            "audio/wav",
            "audio/x-wav",
            "audio/wave",
            "audio/mpeg",  # mp3
            "audio/mp4",  # m4a
            "audio/aac",
            "audio/ogg",
            "audio/flac",
            "application/octet-stream"  # 有些浏览器可能不识别音频类型
        ]
        # 只要 MIME 类型匹配任一允许前缀，就先把它视为音频。
        is_audio = any(content_type.startswith(t) for t in allowed_audio_types)
        
        # 也检查文件扩展名
        original_filename = file.filename or "audio"
        ext = os.path.splitext(original_filename)[1].lower()
        allowed_extensions = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma"]
        
        # MIME 和扩展名都不符合时才拒绝，以兼容浏览器发送 octet-stream 的情况。
        if not is_audio and ext not in allowed_extensions:
            raise HTTPException(
                status_code=400, 
                detail=f"只支持音频文件，支持的格式：{', '.join(allowed_extensions)}"
            )
        
        # 生成唯一文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        
        # 不受支持或缺失的扩展名统一改为 mp3，保证保存后的 URL 有可识别后缀。
        if not ext or ext not in allowed_extensions:
            ext = ".mp3"
        
        filename = f"upload_{timestamp}_{unique_id}{ext}"
        file_path = AUDIOS_DIR / filename
        
        # 保存文件
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        # 返回静态资源 URL，而不是暴露服务器上的绝对文件路径。
        audio_url = f"/storage/audios/{filename}"
        return {"url": audio_url, "filename": filename}
        
    except HTTPException:
        # 保留上面主动抛出的 400 状态，不要把用户输入错误包装成 500。
        raise
    except Exception as e:
        # 磁盘写入等未预期错误统一转换为 FastAPI 的 500 响应。
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@router.post("/upload-video")
async def upload_video(file: UploadFile = File(...)):
    """
    上传视频到 storage/videos 目录
    支持的格式：mp4, mov, avi, mkv, webm

    Returns:
        上传后的视频URL（相对路径，如 /storage/videos/xxx.mp4）
    """
    try:
        BASE_DIR = Path(__file__).parent.parent.parent
        VIDEOS_DIR = BASE_DIR / "storage" / "videos"
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

        # 视频校验与音频相同：同时参考客户端 MIME 和文件扩展名。
        content_type = file.content_type or ""
        original_filename = file.filename or "video"
        ext = os.path.splitext(original_filename)[1].lower()
        allowed_extensions = [".mp4", ".mov", ".avi", ".mkv", ".webm"]

        # octet-stream 是浏览器无法识别准确类型时常用的通用二进制 MIME。
        is_video = content_type.startswith("video/") or content_type == "application/octet-stream"
        if not is_video and ext not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"只支持视频文件，支持的格式：{', '.join(allowed_extensions)}"
            )

        # 使用“时间戳 + UUID”生成碰撞概率很低且便于排查的文件名。
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        if not ext or ext not in allowed_extensions:
            ext = ".mp4"

        filename = f"upload_{timestamp}_{unique_id}{ext}"
        file_path = VIDEOS_DIR / filename

        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        # 该相对 URL 由 main.py 中注册的 /storage 静态文件服务提供。
        video_url = f"/storage/videos/{filename}"
        return {"url": video_url, "filename": filename}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@router.post("/chat")
async def chat(request: ChatRequest):
    """
    处理一轮聊天并返回 SSE 流式响应。

    数据会同时流向三个地方：
    1. 通过当前 HTTP 响应返回给发起请求的前端；
    2. 通过 WebSocket 广播给订阅当前画布的其他客户端；
    3. 流结束后汇总文本和工具结果，保存到画布历史。

    若请求包含 canvas_id，WebSocket 事件只广播到对应画布；否则广播到所有连接。
    """
    try:
        # copy() 避免直接修改 Pydantic 请求对象中的原列表。
        messages = request.messages.copy() if request.messages else []
        # Agent 需要完整上下文，所以把本轮消息追加到历史后再传入服务层。
        messages.append({
            "role": "user",
            "content": request.message
        })

        canvas_id = request.canvas_id

        async def stream_and_save():
            """消费 Agent 流，并依次完成响应、广播和历史持久化。

            这是异步生成器：每次 yield 都会让 StreamingResponse 尽快把一个 SSE
            数据块发送给客户端，而不必等待整轮 Agent 执行结束。
            """
            # delta 是增量文本，保存历史前需要重新拼成完整的助手回复。
            assistant_content = ""
            # 收集工具调用结果（图片/视频等媒体 URL）
            tool_results: list[dict] = []

            # WebSocket 订阅者并没有发起当前 HTTP 请求，因此先单独广播用户消息。
            # 直接 await 能保持事件顺序：user_message 一定早于后续 Agent 事件。
            user_event = json.dumps({"type": "user_message", "content": request.message}, ensure_ascii=False)
            if canvas_id:
                await manager.broadcast(canvas_id, user_event)
            else:
                await manager.broadcast_all(user_event)

            # process_chat_stream 会产生形如 "data: {...}\n\n" 的标准 SSE 文本块。
            async for chunk in process_chat_stream(messages, request.session_id):
                # 第一条通路：立即返回给当前 HTTP/SSE 请求的调用者。
                yield chunk

                # 以下处理只针对 SSE data 行；空行等其他格式不会进入业务解析。
                if chunk.startswith("data: "):
                    # 去掉 SSE 协议前缀，得到可用于 WebSocket 和 JSON 解析的正文。
                    data_str = chunk[len("data: "):].strip()
                    if data_str and data_str != "[DONE]":
                        # 第二条通路：将同一事件同步给 WebSocket 订阅者。
                        if canvas_id:
                            await manager.broadcast(canvas_id, data_str)
                        else:
                            await manager.broadcast_all(data_str)

                        # 第三条通路的准备：从事件中汇总稍后要持久化的内容。
                        try:
                            ev = json.loads(data_str)
                            ev_type = ev.get("type")
                            if ev_type == "delta" and ev.get("content"):
                                # 每个 delta 只包含一小段文本，需要按到达顺序拼接。
                                assistant_content += ev["content"]
                            elif ev_type == "tool_result":
                                # content 通常是工具返回的 JSON 字符串，其中包含媒体 URL。
                                # tool_call_id 用来关联此前的 tool_call 事件。
                                tool_results.append({
                                    "tool_call_id": ev.get("tool_call_id"),
                                    "content": ev.get("content"),
                                })
                        except Exception:
                            # 某个事件无法解析时不应中断整个流；客户端仍可继续收到后续事件。
                            pass
                    elif data_str == "[DONE]":
                        # [DONE] 是 SSE 内部结束标记；WebSocket 使用 JSON done 事件表达同一语义。
                        done_event = json.dumps({"type": "done"}, ensure_ascii=False)
                        if canvas_id:
                            await manager.broadcast(canvas_id, done_event)
                        else:
                            await manager.broadcast_all(done_event)

            # 只有 Agent 流自然结束后才执行到这里，再把本轮结果作为一个整体保存。
            try:
                import time
                # 前端的 createdAt 使用毫秒时间戳，因此这里乘以 1000 保持格式一致。
                ts = int(time.time() * 1000)

                # 构建本轮新增的消息（用户 + 助手）
                new_messages = [
                    {"role": "user", "content": request.message},
                    {
                        "role": "assistant",
                        "content": assistant_content,
                        # 若有工具调用结果（图片等），附加到 assistant 消息
                        **({"tool_results": tool_results} if tool_results else {}),
                    },
                ]

                if canvas_id:
                    # 已有项目：加载原有 canvas，追加本轮消息，不新建项目
                    existing_canvases = history_service.get_canvases()
                    # next(..., None) 在找不到对应画布时返回 None，进入下面的降级分支。
                    existing = next((c for c in existing_canvases if c.get("id") == canvas_id), None)
                    if existing:
                        existing_messages = existing.get("messages", [])
                        existing["messages"] = existing_messages + new_messages
                        # history_service 使用同步文件 I/O；放到线程中避免阻塞事件循环。
                        await asyncio.to_thread(history_service.save_canvas, existing)
                    else:
                        # canvas_id 在历史中找不到（可能已删除），退化为新建
                        canvas_to_save = {
                            "id": canvas_id,
                            # 取用户消息前 30 个字符作为默认项目名称。
                            "name": request.message[:30],
                            "createdAt": ts,
                            "messages": new_messages,
                            "data": {"elements": [], "appState": {}, "files": {}},
                        }
                        await asyncio.to_thread(history_service.save_canvas, canvas_to_save)
                else:
                    # 没有 canvas_id：以后端生成的毫秒时间戳作为新画布 ID。
                    new_canvas = {
                        "id": f"canvas-{ts}",
                        "name": request.message[:30],
                        "createdAt": ts,
                        "messages": new_messages,
                        "data": {"elements": [], "appState": {}, "files": {}},
                    }
                    await asyncio.to_thread(history_service.save_canvas, new_canvas)
            except Exception as e:
                # 历史保存失败不再影响已经成功发送给客户端的流式响应。
                logger.warning(f"保存对话历史失败: {e}")

        # StreamingResponse 会消费上面的异步生成器，并保持 HTTP 连接持续输出。
        return StreamingResponse(
            stream_and_save(),
            media_type="text/event-stream",
            headers={
                # 禁止浏览器或中间代理缓存事件流。
                "Cache-Control": "no-cache",
                # 告知客户端复用连接，直到流主动结束。
                "Connection": "keep-alive",
                # 禁止 Nginx 缓冲，否则多个小 delta 可能积累后才一次性到达前端。
                "X-Accel-Buffering": "no",
                "Content-Type": "text/event-stream; charset=utf-8"
            }
        )
    except Exception as e:
        # 这里处理创建流式响应之前发生的异常；流开始后的异常由下游生成器处理。
        raise HTTPException(status_code=500, detail=str(e))

