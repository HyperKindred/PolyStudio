"""将多个视频片段按顺序拼接为一个 MP4 的 LangChain Tool。

本模块分为两层：

``concatenate_videos``
    业务实现层，负责把本地路径、localhost URL 或公网 URL 统一为本地文件，
    使用 MoviePy 对齐分辨率和帧率，然后编码输出 MP4。

``concatenate_videos_tool``
    Agent 适配层，通过 Pydantic Schema 和 ``@tool`` 向 LLM 暴露参数，并把成功
    或失败结果统一序列化为 JSON 字符串。

该工具只做顺序拼接，没有加入淡入淡出等显式转场效果。
"""
import json
import logging
import os
import uuid
import requests
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 工具可能由 FastAPI 导入，也可能单独运行，因此主动定位并加载 backend/.env。
BASE_DIR = Path(__file__).parent.parent.parent
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# 所有下载的输入片段和最终输出都放在 backend/storage/videos。
# main.py 将 storage 挂载为静态目录，因此输出可以通过 /storage/videos/... 访问。
STORAGE_DIR = BASE_DIR / "storage"
VIDEOS_DIR = STORAGE_DIR / "videos"

# 确保视频存储目录存在
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

# Mock 模式跳过下载、MoviePy 和编码，直接返回固定路径，用于离线测试 Agent 调用链。
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
# Mock 视频路径（启用 MOCK_MODE 时必须配置）
MOCK_VIDEO_PATH = os.getenv("MOCK_VIDEO_PATH", "").strip()
if MOCK_MODE and not MOCK_VIDEO_PATH:
    logger.warning(
        "MOCK_MODE=true 时，建议配置 MOCK_VIDEO_PATH。"
        "请在 backend/.env 中设置 MOCK_VIDEO_PATH=/storage/videos/your_video.mp4"
    )

# MoviePy 1.x 和 2.x 的导入路径、部分方法名不同，后续代码同时兼容两代 API。
# 导入失败不会阻止整个后端启动，只把当前 Tool 标记为不可用。
try:
    # moviepy 2.x 推荐直接从 moviepy 导入
    from moviepy import VideoFileClip, concatenate_videoclips
    MOVIEPY_AVAILABLE = True
except Exception:
    try:
        # 兼容 1.x 旧路径
        from moviepy.editor import VideoFileClip, concatenate_videoclips
        MOVIEPY_AVAILABLE = True
    except ImportError:
        MOVIEPY_AVAILABLE = False
        logger.warning("⚠️ moviepy 未安装或版本不兼容，视频拼接功能将不可用。请运行: pip install \"moviepy>=1.0.3\"")


def prepare_video_path(video_url: str) -> Path:
    """
    准备视频文件路径，支持本地路径和 URL
    
    Args:
        video_url: 本地路径（如 /storage/videos/xxx.mp4）或 URL（如 http://localhost:8000/storage/videos/xxx.mp4）
    
    Returns:
        本地文件路径（Path 对象）
    
    Raises:
        FileNotFoundError: 本地文件不存在
        ValueError: URL 下载失败
    """
    # 情况一：应用内部的 /storage URL。去掉开头 / 后与 backend 根目录拼接。
    if video_url.startswith("/storage/"):
        file_path = BASE_DIR / video_url.lstrip("/")
        if not file_path.exists():
            raise FileNotFoundError(f"本地文件不存在: {file_path}")
        logger.info(f"📁 使用本地文件: {file_path}")
        return file_path
    
    # 情况二：localhost 完整 URL 实际仍指向本项目文件，直接映射磁盘路径，
    # 避免后端通过 HTTP 再请求自己一次。
    if video_url.startswith("http://localhost") or video_url.startswith("http://127.0.0.1"):
        # 提取路径部分
        parsed_url = urlparse(video_url)
        local_path = parsed_url.path
        if local_path.startswith("/storage/"):
            file_path = BASE_DIR / local_path.lstrip("/")
            if file_path.exists():
                logger.info(f"📁 从 localhost URL 转换为本地路径: {file_path}")
                return file_path
    
    # 情况三：其余值按公网 URL 处理，先下载成本地文件再交给 MoviePy。
    logger.info(f"📥 下载视频: {video_url}")
    try:
        # stream=True 不一次性把整个视频读进内存；300 秒适配较大的远端视频。
        response = requests.get(video_url, timeout=300, stream=True)
        # 将 4xx/5xx 响应转换成异常，进入统一下载失败分支。
        response.raise_for_status()
        
        # URL 路径没有扩展名时默认按 mp4 保存；时间戳和 UUID 避免重名。
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        ext = os.path.splitext(urlparse(video_url).path)[1] or ".mp4"
        filename = f"temp_{timestamp}_{unique_id}{ext}"
        temp_path = VIDEOS_DIR / filename
        
        # 分块写入能控制内存占用，适合体积较大的视频文件。
        with open(temp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        logger.info(f"✅ 视频已下载到: {temp_path}")
        return temp_path
        
    except Exception as e:
        logger.error(f"❌ 下载视频失败: {str(e)}")
        raise ValueError(f"无法下载视频: {str(e)}")


def concatenate_videos(
    video_urls: List[str],
    output_filename: Optional[str] = None
) -> str:
    """
    将多个视频片段拼接为一个完整视频
    
    Args:
        video_urls: 视频路径列表（支持本地路径和URL）
        output_filename: 输出文件名（可选，如果不提供则自动生成）
    
    Returns:
        拼接后的视频路径（相对路径，如 /storage/videos/xxx.mp4）
    
    Raises:
        ValueError: 如果 moviepy 未安装或视频列表为空
        FileNotFoundError: 如果视频文件不存在
    """
    # 业务函数也自行校验依赖，因此即使绕过 Tool 包装层直接调用也有明确错误。
    if not MOVIEPY_AVAILABLE:
        raise ValueError(
            "moviepy 未安装，无法拼接视频。"
            "请运行: pip install moviepy"
        )
    
    # 单个视频不需要拼接，同时避免 MoviePy 对空列表产生难以理解的异常。
    if not video_urls or len(video_urls) < 2:
        raise ValueError("至少需要2个视频片段才能拼接")
    
    try:
        logger.info(f"🎬 开始拼接 {len(video_urls)} 个视频片段")
        
        # 第一阶段：把三类输入地址全部标准化为存在的本地 Path。
        video_paths = []
        for i, video_url in enumerate(video_urls, 1):
            logger.info(f"  处理片段 {i}/{len(video_urls)}: {video_url}")
            video_path = prepare_video_path(video_url)
            video_paths.append(video_path)
        
        # 第二阶段：打开视频文件，并以第一个片段作为输出规格基准。
        clips = []
        # first_clip 当前只用于保留首片段引用；目标规格由 target_size/target_fps 保存。
        first_clip = None
        for i, video_path in enumerate(video_paths):
            logger.info(f"  加载视频片段 {i+1}: {video_path}")
            clip = VideoFileClip(str(video_path))
            
            # 第一个视频决定最终分辨率和帧率，后续片段向它对齐。
            if i == 0:
                first_clip = clip
                target_size = clip.size
                target_fps = clip.fps
                logger.info(f"  目标分辨率: {target_size}, 帧率: {target_fps}")
            
            # 分辨率或 FPS 不一致可能导致直接拼接失败或输出节奏异常，因此先标准化。
            if clip.size != target_size or clip.fps != target_fps:
                logger.info(f"  调整视频 {i+1} 的分辨率和帧率: {clip.size} -> {target_size}, {clip.fps} -> {target_fps}")
                # 通过 hasattr 在运行时适配 MoviePy 2.x 和 1.x 的不同方法名。
                if hasattr(clip, "resized"):
                    clip = clip.resized(target_size)
                else:
                    clip = clip.resize(target_size)
                if hasattr(clip, "with_fps"):
                    clip = clip.with_fps(target_fps)
                else:
                    clip = clip.set_fps(target_fps)
            
            clips.append(clip)
        
        # 第三阶段：按照 video_urls 原始顺序拼接。compose 能兼容片段属性差异，
        # 但前面仍主动统一尺寸和 FPS，使输出更可控。
        logger.info("  正在拼接视频片段...")
        final_clip = concatenate_videoclips(clips, method="compose")
        
        # 调用方未指定名称时生成唯一、可识别的 mp4 文件名。
        if not output_filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = str(uuid.uuid4())[:8]
            output_filename = f"concatenated_{timestamp}_{unique_id}.mp4"
        
        # 当前编码参数固定输出 MP4，因此统一补充 .mp4 后缀。
        if not output_filename.endswith(".mp4"):
            output_filename += ".mp4"
        
        output_path = VIDEOS_DIR / output_filename
        
        # 第四阶段：重新编码为浏览器普遍支持的 H.264 视频 + AAC 音频。
        logger.info(f"  正在保存拼接后的视频: {output_path}")
        final_clip.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            fps=target_fps,
            preset="medium"
        )
        
        # VideoFileClip 持有文件句柄和 ffmpeg 资源，编码结束后必须主动关闭。
        for clip in clips:
            clip.close()
        final_clip.close()
        
        # 对上层返回 HTTP 静态资源路径，而不是暴露 Windows 绝对磁盘路径。
        http_path = f"/storage/videos/{output_filename}"
        logger.info(f"✅ 视频拼接完成: {http_path}")
        logger.info(f"   总时长: {final_clip.duration:.2f}秒")
        logger.info(f"   分辨率: {target_size}")
        
        return http_path
        
    except Exception as e:
        logger.error(f"❌ 视频拼接失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise


class ConcatenateVideosInput(BaseModel):
    """Agent 调用视频拼接 Tool 时使用的参数 Schema。"""

    # 字段 description 会进入 Tool 定义，帮助 LLM 正确排列路径和选择可选文件名。
    video_urls: List[str] = Field(description="视频路径列表，按顺序拼接。支持本地路径（如 /storage/videos/xxx.mp4）或 URL")
    output_filename: Optional[str] = Field(default=None, description="输出文件名（可选，如果不提供则自动生成）")


@tool("concatenate_videos", args_schema=ConcatenateVideosInput)
def concatenate_videos_tool(
    video_urls: List[str],
    output_filename: Optional[str] = None
) -> str:
    """
    将多个视频片段拼接为一个完整视频。
    
    **使用场景**：
    - 长视频生成：当需要生成超过单个片段时长限制（4-12秒）的长视频时
    - 视频组合：将多个独立生成的视频片段组合成完整作品
    
    **典型工作流**（长视频生成）：
    当用户要求生成超过单个片段时长限制的长视频时：
    1. 拆分镜头：根据总时长拆分为多个镜头场景
    2. 生成图片：为每个镜头生成首帧图片（generate_volcano_image）
    3. 生成视频：基于图片生成视频片段（generate_volcano_video，mode="image"）
    4. 拼接视频：使用本工具将所有片段按顺序拼接
    
    **参数说明**：
    - video_urls: 视频路径列表，按故事顺序排列。支持：
      * 本地路径：/storage/videos/xxx.mp4
      * localhost URL：http://localhost:8000/storage/videos/xxx.mp4
      * 公网 URL：https://example.com/video.mp4（会自动下载）
    - output_filename: 输出文件名（可选），如不提供则自动生成
    
    **技术细节**：
    - 自动统一所有片段的分辨率和帧率（使用第一个视频的参数）
    - 支持不同格式的视频（会自动转换）
    - 输出格式：MP4 (H.264 + AAC)
    
    Args:
        video_urls: 视频路径列表，按顺序拼接
        output_filename: 输出文件名（可选）
    
    Returns:
        拼接后的视频路径的JSON字符串（格式：{"video_url": "/storage/videos/xxx.mp4", ...}）
    """
    # @tool 将普通 Python 函数包装成 LangChain Tool；名称和函数 docstring 会暴露给 LLM。
    # Mock 与真实分支返回相同的核心字段，前端和后续 Tool 无需区分处理。
    if MOCK_MODE:
        logger.info(f"🎭 [MOCK模式] 拼接视频: {len(video_urls)} 个片段")
        result = {
            'video_url': MOCK_VIDEO_PATH or "/storage/videos/mock_concatenated.mp4",
            'local_path': MOCK_VIDEO_PATH or "/storage/videos/mock_concatenated.mp4",
            'video_count': len(video_urls),
            'output_filename': output_filename or "mock_concatenated.mp4",
            'mock': True,
            'message': '[MOCK] 视频已拼接并保存到本地'
        }
        return json.dumps(result, ensure_ascii=False)
    
    try:
        # Tool 层把依赖错误转成 JSON，而不是抛出异常终止整个 ReAct 图。
        if not MOVIEPY_AVAILABLE:
            error_msg = "moviepy 未安装，无法拼接视频。请运行: pip install moviepy"
            logger.error(error_msg)
            return json.dumps({
                'error': error_msg
            }, ensure_ascii=False)
        
        logger.info(f"🎬 开始拼接 {len(video_urls)} 个视频片段")
        
        # 具体路径准备、规格对齐和编码全部委托给业务实现函数。
        output_path = concatenate_videos(video_urls, output_filename)
        
        # LangChain 会把这个 JSON 字符串放入 ToolMessage.content，模型和前端均可解析。
        result = {
            'video_url': output_path,
            'local_path': output_path,
            'video_count': len(video_urls),
            'output_filename': os.path.basename(output_path),
            'message': f'成功拼接 {len(video_urls)} 个视频片段'
        }
        
        return json.dumps(result, ensure_ascii=False)
        
    except Exception as e:
        # 业务函数保留异常语义，Tool 包装层再统一转换成模型可读取的错误 JSON。
        error_msg = f"视频拼接失败: {str(e)}"
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())
        return json.dumps({
            'error': error_msg
        }, ensure_ascii=False)


if __name__ == "__main__":
    # 直接运行文件时执行本地冒烟测试；被 agent_service 导入时不会进入此分支。
    import sys

    # 加载环境变量
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
        print(f"✅ 已加载环境变量: {ENV_PATH}")
    else:
        print(f"⚠️  未找到 .env 文件: {ENV_PATH}")
    
    logging.basicConfig(level=logging.INFO)
    
    # 如果 moviepy 未安装，提示使用当前解释器安装并退出
    if not MOVIEPY_AVAILABLE:
        print("❌ moviepy 未安装，无法拼接视频。")
        print("请使用当前解释器安装：python -m pip install \"moviepy>=1.0.3\"")
        sys.exit(1)
    
    # 写死的测试视频列表（确保文件存在）
    video_urls = [
        "/storage/videos/volcano_20260115_182750_2c304555_喜庆红色背景金色祥云环绕一匹金色骏马从画面左侧奔腾至右侧.mp4",
        "/storage/videos/volcano_20260117_225006_b7514562_在阳光下熠熠生辉穿梭其中.mp4",
    ]

    print(f"\n测试拼接 {len(video_urls)} 个视频:")
    for i, url in enumerate(video_urls, 1):
        print(f"  {i}. {url}")
    
    result = concatenate_videos_tool.invoke({
        "video_urls": video_urls
    })
    print("\n拼接结果:")
    print(result)
