"""火山引擎 Seedream 图片生成与编辑 Tool。

本模块向 LangGraph Agent 暴露两个 LangChain Tool：

``generate_volcano_image``
    文本 -> 火山引擎图片生成 API -> 下载到本地 -> 返回 /storage URL。

``edit_volcano_image``
    本地源图片 -> Base64 Data URL + 编辑指令 -> 图片编辑 API -> 下载到本地。

Agent 只接触 Tool 的名称、描述、参数 Schema 和返回字符串；HTTP 请求、文件转换、
色彩空间归一化及本地持久化都封装在本模块内部。
"""
import json
import logging
import os
import requests
import uuid
import base64
from datetime import datetime
from pathlib import Path
from typing import Tuple, Union
from urllib.parse import urlparse
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Pillow 是可选的色彩处理依赖。导入失败时仍允许 Tool 工作，只跳过 sRGB 归一化。
# 这样外部 API 可用但图像处理依赖异常时，不会导致整个 Agent 在导入阶段失败。
try:
    from PIL import Image, ImageCms  # type: ignore
    from io import BytesIO
except Exception:  # pragma: no cover
    Image = None
    ImageCms = None
    BytesIO = None  # type: ignore
    logger.warning("⚠️ 未安装 Pillow：将无法进行 sRGB 归一化，<img> 与 Excalidraw(canvas) 可能出现颜色差异。请安装 requirements.txt 后重启后端。")

# 本文件可能被 FastAPI 导入，也可能作为脚本直接运行，因此主动定位 backend/.env。
# main.py 虽然也会 load_dotenv()，这里再次加载能降低该 Tool 对应用启动顺序的依赖。
BASE_DIR = Path(__file__).parent.parent.parent
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# 这些配置在模块导入时读取一次；修改 .env 后需要重启后端才能刷新。
VOLCANO_API_KEY = os.getenv("VOLCANO_API_KEY", "").strip()
VOLCANO_BASE_URL = os.getenv("VOLCANO_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").strip()
VOLCANO_IMAGE_MODEL = os.getenv("VOLCANO_IMAGE_MODEL", "seedream-4.5").strip()
# 若编辑模型不同，可单独配置；缺省复用生成模型
VOLCANO_EDIT_MODEL = os.getenv("VOLCANO_EDIT_MODEL", VOLCANO_IMAGE_MODEL).strip()
# Mock 模式不请求外部服务，直接返回指定测试图片，便于离线调试完整 Agent 事件链。
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"
# Mock 图片路径（启用 MOCK_MODE 时必须配置）
MOCK_IMAGE_PATH = os.getenv("MOCK_IMAGE_PATH", "").strip()
if MOCK_MODE and not MOCK_IMAGE_PATH:
    # 在导入阶段尽早暴露不完整的 Mock 配置，避免运行 Tool 后才得到模糊错误。
    raise RuntimeError(
        "MOCK_MODE=true 时，必须配置 MOCK_IMAGE_PATH。"
        "请在 backend/.env 中设置 MOCK_IMAGE_PATH=/storage/images/your_image.png"
    )

# API 返回的 URL 通常是远端或临时地址，必须下载到项目自己的持久化目录。
STORAGE_DIR = BASE_DIR / "storage"
IMAGES_DIR = STORAGE_DIR / "images"

# 确保图片存储目录存在
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# 对用户和 LLM 暴露容易理解的宽高比，调用 API 前再转换为确定的像素尺寸。
ASPECT_RATIO_MAP = {
    "1:1": (2048, 2048),
    "4:3": (2304, 1728),
    "3:4": (1728, 2304),
    "16:9": (2560, 1440),
    "9:16": (1440, 2560),
    "3:2": (2496, 1664),
    "2:3": (1664, 2496),
    "21:9": (3024, 1296),
}


def parse_size(size: str) -> str:
    """
    解析尺寸参数，支持宽高比枚举、自定义格式或API格式
    
    Args:
        size: 宽高比字符串（如 "16:9", "4:3"）、自定义格式（如 "1024x1024"）或API格式（如 "2K"）
    
    Returns:
        返回API可接受的尺寸字符串格式（如 "2K" 或 "2048x2048"）
    """
    # API 原生简写只需统一大小写，不需要换算。
    if size.upper() in ["2K", "4K", "1K"]:
        return size.upper()
    
    # 常用宽高比通过映射转换，例如 16:9 -> 2560x1440。
    if size in ASPECT_RATIO_MAP:
        width, height = ASPECT_RATIO_MAP[size]
        return f"{width}x{height}"
    
    # 自定义宽高同时兼容小写 x 和大写 X，并验证两侧能否转换成整数。
    if "x" in size or "X" in size:
        parts = size.replace("X", "x").split("x")
        if len(parts) == 2:
            try:
                width = int(parts[0].strip())
                height = int(parts[1].strip())
                return f"{width}x{height}"
            except ValueError:
                pass
    
    # 非法输入不让整个 Tool 失败，而是记录警告并使用稳定的默认尺寸。
    logger.warning(f"无法解析尺寸参数: {size}，使用默认 1:1 (2048x2048)")
    width, height = ASPECT_RATIO_MAP["1:1"]
    return f"{width}x{height}"


def prepare_image_input(image_url: str) -> Union[str, list]:
    """
    准备图片输入，只处理本地文件（转Base64），不支持公网URL（会过期）
    
    Args:
        image_url: 本地路径（如 /storage/images/xxx.jpg）或 localhost URL（如 http://localhost:8000/storage/images/xxx.jpg）
    
    Returns:
        Base64编码字符串
    
    Raises:
        FileNotFoundError: 本地文件不存在
        ValueError: 不支持公网URL（会过期）
    """
    # 分支一：前端和 Tool 返回值常使用 /storage/... 形式的应用内 URL。
    if image_url.startswith("/storage/"):
        # 去掉开头的 / 后与 backend 根目录拼接，得到真实磁盘路径。
        file_path = BASE_DIR / image_url.lstrip("/")
        if not file_path.exists():
            raise FileNotFoundError(f"本地文件不存在: {file_path}")
        
        logger.info(f"📁 读取本地文件: {file_path}")
        
        # 读取文件
        with open(file_path, "rb") as f:
            image_data = f.read()
        
        # Data URL 的 MIME 子类型由扩展名推断，API 据此解释 Base64 内容。
        ext = file_path.suffix.lower()
        if ext in [".jpg", ".jpeg"]:
            image_format = "jpeg"
        elif ext == ".png":
            image_format = "png"
        elif ext == ".webp":
            image_format = "webp"
        elif ext == ".bmp":
            image_format = "bmp"
        elif ext in [".tiff", ".tif"]:
            image_format = "tiff"
        elif ext == ".gif":
            image_format = "gif"
        else:
            # 默认使用jpeg
            image_format = "jpeg"
            logger.warning(f"未知图片格式 {ext}，使用 jpeg")
        
        # b64encode 返回 bytes，decode 后才能嵌入 JSON 字符串。
        base64_data = base64.b64encode(image_data).decode("utf-8")
        # 最终格式示例：data:image/png;base64,iVBORw0KGgo...
        base64_string = f"data:image/{image_format};base64,{base64_data}"
        
        logger.info(f"✅ 已转换为Base64格式: {image_format}, 大小={len(image_data)} bytes")
        return base64_string
    
    # 分支二：完整 localhost URL 仍指向本项目文件，不通过 HTTP 下载，而是取 path
    # 部分映射回本地 storage，避免后端绕一圈请求自己。
    parsed = urlparse(image_url)
    if parsed.hostname in ["localhost", "127.0.0.1", "0.0.0.0"] or (parsed.hostname and "localhost" in parsed.hostname):
        # localhost URL，读取本地文件
        if parsed.path.startswith("/storage/"):
            file_path = BASE_DIR / parsed.path.lstrip("/")
            if not file_path.exists():
                raise FileNotFoundError(f"本地文件不存在: {file_path}")
            
            logger.info(f"📁 从localhost URL读取本地文件: {file_path}")
            
            # 读取文件并转换为Base64
            with open(file_path, "rb") as f:
                image_data = f.read()
            
            ext = file_path.suffix.lower()
            if ext in [".jpg", ".jpeg"]:
                image_format = "jpeg"
            elif ext == ".png":
                image_format = "png"
            elif ext == ".webp":
                image_format = "webp"
            elif ext == ".bmp":
                image_format = "bmp"
            elif ext in [".tiff", ".tif"]:
                image_format = "tiff"
            elif ext == ".gif":
                image_format = "gif"
            else:
                image_format = "jpeg"
            
            base64_data = base64.b64encode(image_data).decode("utf-8")
            base64_string = f"data:image/{image_format};base64,{base64_data}"
            
            logger.info(f"✅ 已转换为Base64格式: {image_format}, 大小={len(image_data)} bytes")
            return base64_string
    
    # 公网源图被主动拒绝：它可能过期、需要鉴权，或被第三方限制访问。
    # 项目约定先把媒体上传/保存到 /storage，再交给编辑 Tool。
    raise ValueError(
        f"不支持公网URL（会过期）: {image_url[:50]}...\n"
        f"请使用本地路径（如 /storage/images/xxx.jpg）或 localhost URL（如 http://localhost:8000/storage/images/xxx.jpg）"
    )


def download_and_save_image(image_url: str, prompt: str = "") -> str:
    """
    下载图片并保存到本地
    
    Args:
        image_url: 图片URL
        prompt: 提示词（用于生成文件名）
    
    Returns:
        本地文件路径（相对路径）
    """
    try:
        logger.info(f"📥 开始下载图片: {image_url}")
        
        # requests.get 是同步下载；timeout 防止远端地址无响应时无限等待。
        response = requests.get(image_url, timeout=60)
        # 4xx/5xx 状态在这里转换为异常，统一进入本函数的 except 分支。
        response.raise_for_status()
        
        # 从URL获取文件扩展名，如果没有则默认为png
        parsed_url = urlparse(image_url)
        path = parsed_url.path
        ext = os.path.splitext(path)[1] or ".png"
        if not ext.startswith("."):
            ext = ".png"
        
        # “时间戳 + UUID”避免重名；短 Prompt 片段让文件仍具有人工可读性。
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        # 删除路径分隔符等特殊字符，避免 Prompt 形成非法或危险文件名。
        safe_prompt = "".join(c if c.isalnum() or c in (" ", "-", "_") else "" for c in prompt[:30])
        safe_prompt = safe_prompt.replace(" ", "_")
        filename = f"volcano_{timestamp}_{unique_id}_{safe_prompt}{ext}" if safe_prompt else f"volcano_{timestamp}_{unique_id}{ext}"
        
        file_path = IMAGES_DIR / filename

        # saved 表示是否已经通过 Pillow 成功保存；失败时会走原始字节回退分支。
        saved = False
        if Image is not None and BytesIO is not None:
            try:
                # BytesIO 把响应字节包装成类文件对象，Pillow 可以直接从内存读取。
                im = Image.open(BytesIO(response.content))
                im.load()

                # 某些生成图片携带非 sRGB ICC Profile；不同渲染器可能呈现不同颜色。
                # 如果存在 Profile，先把像素转换到 sRGB，再移除嵌入的 Profile 元数据。
                if ImageCms is not None:
                    icc = getattr(im, "info", {}).get("icc_profile")
                    if icc:
                        try:
                            src_profile = ImageCms.ImageCmsProfile(BytesIO(icc))
                            dst_profile = ImageCms.createProfile("sRGB")
                            output_mode = "RGBA" if (
                                im.mode in ("RGBA", "LA") or ("transparency" in getattr(im, "info", {}))
                            ) else "RGB"
                            im = ImageCms.profileToProfile(im, src_profile, dst_profile, outputMode=output_mode)
                        except Exception:
                            # ICC 转换失败：退化为普通模式转换（不抛）
                            pass

                # 彻底去掉 ICC（避免浏览器两条渲染链路按不同 profile 解释）
                try:
                    if getattr(im, "info", None) and "icc_profile" in im.info:
                        im.info.pop("icc_profile", None)
                except Exception:
                    pass

                # 保存格式策略：
                # - 若图片不透明：统一存为 JPEG（去掉 PNG 的 gAMA/sRGB/cHRM 等色彩块差异，减少 <img> vs canvas 偏色）
                # - 若图片含透明：存为 PNG（保留 alpha）
                has_alpha = im.mode in ("RGBA", "LA") or ("transparency" in getattr(im, "info", {}))
                is_transparent = False
                if has_alpha:
                    try:
                        alpha = im.getchannel("A")
                        lo, hi = alpha.getextrema()
                        is_transparent = lo < 255
                    except Exception:
                        is_transparent = True

                if not is_transparent:
                    # 不透明图片转成 JPEG，减少 PNG 色彩块导致的渲染差异。
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    filename = os.path.splitext(filename)[0] + ".jpg"
                    file_path = IMAGES_DIR / filename
                    im.save(file_path, format="JPEG", quality=95, optimize=True, progressive=True)
                else:
                    # 含透明像素时必须保留 Alpha，因此保存为 PNG。
                    filename = os.path.splitext(filename)[0] + ".png"
                    file_path = IMAGES_DIR / filename
                    if im.mode not in ("RGBA", "RGB"):
                        im = im.convert("RGBA")
                    im.save(file_path, format="PNG", optimize=True)

                saved = True
                logger.info("🎛️ 已进行 sRGB 归一化并保存（移除 ICC profile）")
            except Exception as e:
                logger.warning(f"⚠️ sRGB 归一化失败，回退为原始字节保存: {e}")

        if not saved:
            # Pillow 不可用或归一化失败时，至少保存 API 返回的原始图片字节。
            with open(file_path, "wb") as f:
                f.write(response.content)
        
        # 返回HTTP访问路径（以/storage/开头，前端可以直接使用）
        http_path = f"/storage/images/{filename}"
        logger.info(f"✅ 图片已保存到本地: {file_path}")
        logger.info(f"   可通过HTTP访问: {http_path}")
        return http_path
        
    except Exception as e:
        logger.error(f"❌ 下载图片失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        # 下载失败时返回原始 URL，让 Agent/前端仍有机会使用远端结果。
        # 因此调用方不能仅凭返回值判断图片一定已经保存到本地。
        return image_url


class GenerateVolcanoImageInput(BaseModel):
    """文生图 Tool 的参数 Schema，会作为工具定义提供给 LLM。"""

    # Field.description 不只是接口文档，也会帮助 LLM 理解字段含义并生成参数。
    prompt: str = Field(description="图像生成的提示词，详细描述想要生成的图像内容，支持中英文")
    size: str = Field(default="1:1", description="图片尺寸，支持宽高比枚举（1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, 21:9）或自定义格式（如 2048x2048），默认 1:1")


@tool("generate_volcano_image", args_schema=GenerateVolcanoImageInput)
def generate_volcano_image_tool(prompt: str, size: str = "1:1") -> str:
    """
    火山引擎 AI 绘画（图片生成）服务，使用 Seedream 4.0-4.5 API 生成图像。
    输入文本描述，返回基于文本信息绘制的图片 URL。
    
    Args:
        prompt: 图像生成的提示词（支持中英文）
        size: 图片尺寸，支持宽高比枚举（1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, 21:9）或自定义格式（如 2048x2048），默认 1:1
    
    Returns:
        生成的图像URL的JSON字符串或错误信息
    """
    # @tool 把普通 Python 函数包装为 LangChain Tool；显式名称是模型看到并调用的名称。
    # Mock 分支保持与真实分支相同的返回字段，前端无需增加特殊处理。
    if MOCK_MODE:
        logger.info(f"🎭 [MOCK模式] 生成图像: prompt={prompt}, size={size}")
        result = {
            'image_url': MOCK_IMAGE_PATH,
            'original_url': MOCK_IMAGE_PATH,
            'local_path': MOCK_IMAGE_PATH,
            'prompt': prompt,
            'provider': 'volcano',
            'mock': True,
            'message': '[MOCK] 图片已生成并保存到本地'
        }
        return json.dumps(result, ensure_ascii=False)
    
    try:
        # Tool 通过返回错误字符串告知 Agent，而不是抛异常终止整个 LangGraph。
        if not VOLCANO_API_KEY:
            return "Error generating image: 未配置 VOLCANO_API_KEY（请在 backend/.env 设置，可参考 env.example）"
        
        # 解析尺寸参数
        size_value = parse_size(size)
        logger.info(f"🎨 开始使用火山引擎生成图像: prompt={prompt}, size={size} -> {size_value}")

        # rstrip('/') 防止配置值末尾已有 / 时拼出双斜杠。
        url = f"{VOLCANO_BASE_URL.rstrip('/')}/images/generations"
        
        # 当前 Tool 固定一次生成一张，并要求 API 返回可下载 URL 而非 Base64。
        payload = {
            "model": VOLCANO_IMAGE_MODEL,
            "prompt": prompt,
            "size": size_value,
            "n": 1,
            "response_format": "url",  # 返回图片URL
            "stream": False,
            "watermark": True
        }
        
        headers = {
            "Authorization": f"Bearer {VOLCANO_API_KEY}",
            "Content-Type": "application/json"
        }
        
        logger.info(f"🚀 调用火山引擎生成 API")
        logger.info(f"   URL: {url}")
        logger.info(f"   请求参数: {json.dumps(payload, ensure_ascii=False, indent=2)}")
        
        # json=payload 会自动 JSON 序列化请求体；生成任务最多等待 120 秒。
        response = requests.post(url, json=payload, headers=headers, timeout=120)
        
        if response.status_code != 200:
            error_msg = f"API调用失败: status={response.status_code}, body={response.text}"
            logger.error(f"❌ {error_msg}")
            return f"Error generating image: {error_msg}"
            
        # 成功响应从 JSON 文本解析成 Python 字典。
        data = response.json()
        logger.info(f"📥 API响应: {json.dumps(data, ensure_ascii=False)}")
        
        # 兼容不同版本/兼容层可能返回的 data、images 或顶层 url 三种结构。
        image_url = None
        
        if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
            image_url = data["data"][0].get("url")
        elif "images" in data and isinstance(data["images"], list) and len(data["images"]) > 0:
            image_url = data["images"][0].get("url")
        elif "url" in data:
            image_url = data["url"]
        
        if not image_url:
            return f"Error: No image URL in response. Response: {json.dumps(data)}"
        
        # 将供应商 URL 转成本项目稳定的 /storage URL，避免临时链接过期。
        local_path = download_and_save_image(image_url, prompt)
        
        # Tool 返回字符串而非 dict，LangGraph 会把它放进 ToolMessage.content。
        # image_url 给前端使用；original_url 保留供应商结果，便于排查问题。
        result = {
            'image_url': local_path,
            'original_url': image_url,
            'local_path': local_path,
            'prompt': prompt,
            'provider': 'volcano',
            'message': '图片已生成并保存到本地'
        }
        
        result_json = json.dumps(result, ensure_ascii=False)
        logger.info(f"✅ 火山引擎图像生成成功: 已保存到本地 {local_path}")
        return result_json
        
    except Exception as e:
        logger.error(f"❌ 火山引擎图像生成失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error generating image: {str(e)}"


class EditVolcanoImageInput(BaseModel):
    """图片编辑 Tool 的参数 Schema，比文生图多一个必填源图片字段。"""
    prompt: str = Field(description="图像编辑的提示词，详细描述想要达到的效果，支持中英文")
    image_url: str = Field(description="需要编辑的源图片URL或本地路径（/storage/images/...）")
    size: str = Field(default="1:1", description="输出图片尺寸，支持宽高比枚举（1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, 21:9）或自定义格式（如 2048x2048），默认 1:1")


@tool("edit_volcano_image", args_schema=EditVolcanoImageInput)
def edit_volcano_image_tool(prompt: str, image_url: str, size: str = "1:1") -> str:
    """
    火山引擎图片编辑服务（Seedream 4.0-4.5 API），基于已有图片和提示词生成新的图片，如保持角色一致性，场景一致性则使用edit_volcano_image_tool工具。

    Args:
        prompt: 编辑提示词（支持中英文）
        image_url: 原图URL或本地路径（如 /storage/images/xxx.png）
        size: 输出图片尺寸，支持宽高比枚举（1:1, 4:3, 3:4, 16:9, 9:16, 3:2, 2:3, 21:9）或自定义格式（如 2048x2048），默认 1:1

    Returns:
        生成的图像URL的JSON字符串或错误信息
    """
    # 编辑 Tool 的总体流程与文生图相同，主要区别是请求体中包含处理后的源图片。
    if MOCK_MODE:
        logger.info(f"🎭 [MOCK模式] 编辑图像: prompt={prompt}, image_url={image_url}, size={size}")
        result = {
            'image_url': MOCK_IMAGE_PATH,
            'original_url': MOCK_IMAGE_PATH,
            'local_path': MOCK_IMAGE_PATH,
            'prompt': prompt,
            'source_image': image_url,
            'provider': 'volcano',
            'mock': True,
            'message': '[MOCK] 图片已编辑并保存到本地'
        }
        return json.dumps(result, ensure_ascii=False)
    
    try:
        if not VOLCANO_API_KEY:
            return "Error editing image: 未配置 VOLCANO_API_KEY（请在 backend/.env 设置，可参考 env.example）"

        # 解析尺寸参数
        size_value = parse_size(size)
        logger.info(f"🖌️ 开始使用火山引擎编辑图像: prompt={prompt}, image_url={image_url}, size={size} -> {size_value}")

        # 当前 prepare_image_input 实际支持 /storage 路径和 localhost URL，均转为 Data URL；
        # 公网 URL 会被拒绝，以避免临时链接失效。
        image_input = prepare_image_input(image_url)

        # Seedream 生成和编辑共用 generations 端点，通过额外的 image 字段区分编辑。
        url = f"{VOLCANO_BASE_URL.rstrip('/')}/images/generations"

        payload = {
            "model": VOLCANO_EDIT_MODEL,
            "prompt": prompt,
            "image": image_input,  # 可以是URL字符串、Base64字符串或数组
            "size": size_value,
            "response_format": "url",
            "stream": False,
            "watermark": True
        }

        headers = {
            "Authorization": f"Bearer {VOLCANO_API_KEY}",
            "Content-Type": "application/json"
        }

        # 浅拷贝只用于日志脱敏，真实 payload 中仍保留完整图片数据。
        payload_for_log = payload.copy()
        if isinstance(payload_for_log.get("image"), str):
            if payload_for_log["image"].startswith("data:image"):
                payload_for_log["image"] = "data:image/...;base64,<Base64数据已隐藏>"
            elif payload_for_log["image"].startswith("http"):
                payload_for_log["image"] = "<公网URL已隐藏>"
        
        logger.info(f"🚀 调用火山引擎编辑 API: model={payload['model']}, url={url}")
        logger.info(f"   请求参数: {json.dumps(payload_for_log, ensure_ascii=False, indent=2)}， 原始URL: {image_url}")

        response = requests.post(url, json=payload, headers=headers, timeout=120)

        if response.status_code != 200:
            error_msg = f"API调用失败: status={response.status_code}, body={response.text}"
            logger.error(f"❌ {error_msg}")
            return f"Error editing image: {error_msg}"

        data = response.json()
        logger.info(f"📥 API响应: {json.dumps(data, ensure_ascii=False)}")

        # 编辑接口理论上可能返回多张；当前业务只取第一张作为最终结果。
        image_urls = []
        if "data" in data and isinstance(data["data"], list):
            image_urls = [item.get("url") for item in data["data"] if item.get("url")]
        elif "images" in data and isinstance(data["images"], list):
            image_urls = [item.get("url") for item in data["images"] if item.get("url")]
        elif "url" in data:
            image_urls = [data["url"]]

        if not image_urls:
            return f"Error: No image URL in response. Response: {json.dumps(data)}"

        new_image_url = image_urls[0]
        local_path = download_and_save_image(new_image_url, prompt)

        result = {
            "image_url": local_path,
            "original_url": new_image_url,
            "local_path": local_path,
            "prompt": prompt,
            "source_image": image_url,
            "provider": "volcano",
            "message": "图片已编辑并保存到本地",
        }

        result_json = json.dumps(result, ensure_ascii=False)
        logger.info(f"✅ 火山引擎图像编辑成功: 已保存到本地 {local_path}")
        return result_json

    except Exception as e:
        logger.error(f"❌ 火山引擎图像编辑失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error editing image: {str(e)}"


if __name__ == "__main__":
    # 直接运行此文件时执行本地冒烟测试；被 Agent 导入时不会进入该分支。
    from dotenv import load_dotenv
    from pathlib import Path
    
    # 加载 .env 文件（从 backend 目录）
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ 已加载环境变量: {env_path}")
    else:
        print(f"⚠️  未找到 .env 文件: {env_path}")
        print("   请确保已配置环境变量或创建 .env 文件")
    
    logging.basicConfig(level=logging.INFO)
    
    # 测试生成图片
    # print("\n测试 generate_volcano_image 工具...")
    # result = generate_volcano_image_tool.invoke({
    #     "prompt": "美丽的日落",
    #     "size": "16:9",
    #     "num_images": 1
    # })
    # print("生成结果:", result)
    
    # 测试编辑图片（需要先有生成的图片URL）
    # print("\n测试 edit_volcano_image 工具...")
    result = edit_volcano_image_tool.invoke({
        "prompt": "让它变成日出",
        "image_url": "/storage/images/volcano_20251223_231147_3c85e4db_美丽的日落.jpg",
        "size": "4:3"
    })
    print("编辑结果:", result)

