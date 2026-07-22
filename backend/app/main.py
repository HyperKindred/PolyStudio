"""PolyStudio 后端应用入口。

这个模块负责组装和启动 Web 服务，而不直接实现聊天、媒体生成等业务：
1. 加载环境变量并初始化日志；
2. 创建 FastAPI 应用和中间件；
3. 准备本地存储、Workspace 等运行目录；
4. 挂载静态资源并注册 API 路由；
5. 提供画布事件同步所需的 WebSocket 端点。
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.routers import chat, settings
import os
from dotenv import load_dotenv
from app.utils.logger import setup_logging
from app.services.connection_manager import manager
from app.services import workspace_service

# 从 backend/.env 读取配置并写入当前进程的环境变量。
# 后续 LLM、图片生成等模块会通过 os.getenv() 使用这些配置。
load_dotenv()

# LOG_LEVEL 未配置时使用 INFO；setup_logging 会统一设置控制台和文件日志格式。
log_level = os.getenv("LOG_LEVEL", "INFO")
setup_logging(log_level=log_level)

# FastAPI 实例是整个后端应用的核心对象。
# title 和 version 也会显示在自动生成的 /docs 接口文档中。
app = FastAPI(title="PolyStudio API", version="1.0.0")

# CORS 控制哪些网页来源可以跨域访问此后端。
# 开发时前端和后端端口不同（例如 3000 与 8000），因此需要允许跨域请求。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许任意来源；生产环境应改为明确的前端域名。
    # 允许请求携带 Cookie、Authorization 等身份凭据。
    allow_credentials=True,
    # 允许 GET、POST、DELETE 等全部 HTTP 方法。
    allow_methods=["*"],
    # 允许 Content-Type、Authorization 等全部请求头。
    allow_headers=["*"],
)

# main.py 位于 backend/app，因此 parent.parent 得到 backend 根目录。
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"

# 不同媒体类型分目录保存，后续既便于工具写入，也便于通过 URL 访问。
IMAGES_DIR = STORAGE_DIR / "images"
MODELS_DIR = STORAGE_DIR / "models"
VIDEOS_DIR = STORAGE_DIR / "videos"
AUDIOS_DIR = STORAGE_DIR / "audios"

# parents=True 会创建缺失的父目录；exist_ok=True 允许目录已经存在。
# 这些语句在应用导入阶段运行，确保第一个请求到来前目录已经准备好。
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
MODELS_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
AUDIOS_DIR.mkdir(parents=True, exist_ok=True)

# 创建缺失的 AGENTS.md、IDENTITY.md、MEMORY.md 等 Workspace 默认文件。
# Agent 创建时会读取这些文件，为模型注入身份、行为规则和长期记忆。
workspace_service.ensure_workspace_defaults()

# 把本地 backend/storage 目录映射成浏览器可以访问的 /storage URL。
# 例如磁盘文件 storage/images/demo.png 对应 GET /storage/images/demo.png。
if STORAGE_DIR.exists():
    app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")

# 将两个 APIRouter 挂载到应用，并统一增加 /api 前缀。
# 因此 chat.py 中的 @router.post("/chat") 最终地址是 POST /api/chat。
# tags 只用于在 /docs 中对接口进行分类展示。
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(settings.router, prefix="/api", tags=["settings"])


@app.websocket("/ws/{canvas_id}")
async def websocket_endpoint(websocket: WebSocket, canvas_id: str):
    """让前端订阅指定画布的实时事件。

    连接地址为 ``ws://localhost:8000/ws/{canvas_id}``。当外部客户端调用
    ``/api/chat`` 并携带相同 canvas_id 时，chat 路由会通过 manager 将用户消息、
    Agent 文本和工具结果广播到这里，已打开的画布便能实时更新。
    """
    # accept 并把连接登记到 manager.active_connections[canvas_id] 中。
    await manager.connect(canvas_id, websocket)
    try:
        # 持续等待客户端数据的主要目的，是让协程和 WebSocket 连接保持存活。
        # 当前业务事件主要由后端向客户端广播，并不处理这里收到的文本内容。
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        # 浏览器关闭页面、刷新或网络断开时，从连接管理器中移除失效连接。
        manager.disconnect(canvas_id, websocket)


@app.get("/")
async def root():
    """提供一个简单的根路径响应，用于人工确认 API 服务已经启动。"""
    return {"message": "PolyStudio API", "status": "running"}


@app.get("/health")
async def health():
    """健康检查端点，部署平台或监控程序可以用它判断进程是否可访问。"""
    return {"status": "ok"}


if __name__ == "__main__":
    # 只有执行 `python -m app.main` 或直接运行本文件时才进入这里。
    # 使用 `uvicorn app.main:app` 启动时，Uvicorn 会直接导入上面的 app 对象。
    import uvicorn
    import os
    import sys

    uvicorn.run(
        # "模块路径:变量名"，表示加载 app/main.py 中名为 app 的 FastAPI 实例。
        "app.main:app",
        # 监听所有网络接口；如果只写 127.0.0.1，则只能从本机访问。
        host="0.0.0.0",
        port=8000,
        # 开发模式下 Python 文件变化后自动重启服务。
        reload=True,
        reload_includes=["*.py"],
        log_level="info"
    )

