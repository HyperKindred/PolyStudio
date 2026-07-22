"""PolyStudio 多模态 Agent 的创建与流式执行服务。

本模块是 Agent 的“总装配层”，主要负责两件事：
1. ``create_agent``：把 LLM、Prompt、Tools、Skills 和 Workspace 记忆组装成
   一个 LangGraph ReAct Agent；
2. ``process_chat_stream``：运行 Agent，并把执行过程转换成上层可消费的 SSE 流。

图片、视频、音频、3D 等具体能力由 ``app/tools`` 中的工具实现，本模块只负责
注册和编排它们。
"""
import json
import os
import logging
from typing import List, Dict, Any, AsyncGenerator, Optional
from langgraph.prebuilt import create_react_agent
from app.services.stream_processor import StreamProcessor
from app.services.prompt import get_full_prompt
from app.services import skill_service
from app.tools.volcano_image_generation import generate_volcano_image_tool, edit_volcano_image_tool
from app.tools.model_3d_generation import generate_3d_model_tool
from app.tools.volcano_video_generation import generate_volcano_video_tool
from app.tools.video_concatenation import concatenate_videos_tool
from app.tools.virtual_anchor_generation import detect_face_tool, generate_virtual_anchor_tool
from app.tools.qwen_tts import qwen_voice_design_tool, qwen_voice_cloning_tool
from app.tools.audio_mixing import concatenate_audio_tool, select_bgm_tool, mix_audio_with_bgm_tool
from app.tools.qwen_omni_understanding import qwen_omni_understand_tool
from app.tools.skill_tools import read_skill_file_tool, list_skill_dir_tool, init_skill_tool, write_skill_file_tool, delete_skill_file_tool
from app.tools.workspace_tools import write_memory
from app.llm.factory import create_llm
from app.services import workspace_service

# __name__ 会生成 app.services.agent_service 命名的 logger；具体输出格式由 main.py 配置。
logger = logging.getLogger(__name__)


def create_agent():
    """创建并返回一个可执行的 LangGraph ReAct Agent。

    ReAct Agent 会在每一轮中让 LLM 根据消息和工具描述做决策：直接回答，或者
    生成 Tool Call；工具结果会作为新的观察结果送回 LLM，直到得到最终回复。

    Returns:
        已编译、可以通过 ``astream`` 运行的 LangGraph Agent。
    """

    # 工厂根据 LLM_PROVIDER 选择火山引擎或 SiliconFlow，并统一返回 BaseChatModel。
    # Agent 只依赖 LangChain 的统一模型接口，不需要了解供应商实现细节。
    model = create_llm()

    # 传给 create_react_agent 的每个对象都是 LangChain Tool。
    # LLM 会看到 Tool 的 name、description 和参数 schema，并据此决定是否调用。
    tools = [
        # 旧版通用图片工具目前停用，保留注释便于切换实现。
        # generate_image_tool,
        # edit_image_tool,
        # 图片生成与图片编辑。
        generate_volcano_image_tool,
        edit_volcano_image_tool,
        # 3D 模型生成。
        generate_3d_model_tool,
        # 视频生成与多个视频片段拼接。
        generate_volcano_video_tool,
        concatenate_videos_tool,
        # 人脸检测与虚拟人视频生成。
        detect_face_tool,
        generate_virtual_anchor_tool,
        # Qwen-TTS：按文字设计声音，或根据参考音频克隆声音。
        qwen_voice_design_tool,
        qwen_voice_cloning_tool,
        # 音频片段拼接、背景音乐选择和混音。
        concatenate_audio_tool,
        select_bgm_tool,
        mix_audio_with_bgm_tool,
        # 对已有图片、音频或视频进行多模态理解，而不是生成新媒体。
        qwen_omni_understand_tool,
        # Skill 渐进加载：先暴露 Skill 元数据，命中后再读取完整 SKILL.md 和资源。
        read_skill_file_tool,
        list_skill_dir_tool,
        # Skill 创建、写入和删除，供 skill-creator 工作流使用。
        init_skill_tool,
        write_skill_file_tool,
        delete_skill_file_tool,
        # 允许 Agent 把值得跨会话保留的信息写入 Workspace 长期记忆。
        write_memory,
    ]

    # 这里打印注册结果，便于启动和调试时确认哪些能力真正暴露给了模型。
    logger.info(f"🛠️  注册工具: {[tool.name for tool in tools]}")

    # 除了 create_react_agent 会读取 Tool 元数据外，项目还把工具名称和描述拼入
    # system prompt，用于进一步提醒模型当前可用能力和调用规则。
    tool_descriptions = []
    for tool in tools:
        tool_descriptions.append(f"- {tool.name}: {tool.description}")
    # 把字符串列表合并成适合插入 Prompt 的多行文本。
    tools_list_text = "\n".join(tool_descriptions)

    # Skill Context 默认只包含已启用 Skill 的名称、描述和路径；完整内容按需读取。
    skills_context = skill_service.get_skills_context()
    # Workspace Context 包含身份、用户偏好、行为规则和长期记忆等跨会话信息。
    workspace_context = workspace_service.get_workspace_context()

    # 将基础 SYSTEM_PROMPT、工具清单、Skill 元数据和 Workspace 信息合成最终提示词。
    full_prompt = get_full_prompt(
        tools_list_text=tools_list_text,
        skills_context=skills_context,
        workspace_context=workspace_context,
    )

    # create_react_agent 会构建“模型节点 -> 工具节点 -> 模型节点”的循环图。
    # LangGraph 1.0 返回的是已编译图，可以直接调用 invoke/stream/astream。
    agent = create_react_agent(
        # 图名称主要用于日志、调试和可观测性标识。
        name="polystudio_multimodal_agent",
        model=model,
        tools=tools,
        prompt=full_prompt
    )
    
    logger.info("✅ Agent创建成功")
    return agent


async def process_chat_stream(
    messages: List[Dict[str, Any]],
    session_id: Optional[str] = None
) -> AsyncGenerator[str, None]:
    """运行一轮 Agent，并异步产出 SSE 格式的字符串事件。

    Args:
        messages: OpenAI 风格的消息列表，例如 ``[{"role": "user", "content": "..."}]``。
        session_id: 可选会话 ID，传给 StreamProcessor 用于日志和流状态标识。

    Yields:
        SSE 文本块，例如 ``data: {"type": "delta", ...}\n\n``，以及最终的
        ``data: [DONE]\n\n``。
    """
    try:
        logger.info(f"💬 收到聊天请求: session_id={session_id}, messages_count={len(messages)}")
        
        # 当前实现每次聊天请求都重新创建 Agent，因此会重新读取模型配置、Skill 状态
        # 和 Workspace Context；配置或记忆更新可以在下一轮立即生效。
        agent = create_agent()

        # StreamProcessor 负责把 LangGraph 的 AIMessageChunk、ToolMessage 等对象
        # 转成前端约定的 delta、tool_call、tool_result、error 等 SSE 事件。
        processor = StreamProcessor(session_id)

        # 异步遍历 Agent 的执行流。等待模型或工具时，事件循环仍可处理其他请求。
        async for event in processor.process_stream(agent, messages):
            try:
                # 向上层 chat 路由立即交出一个事件，而不是等待完整回复生成。
                yield event
            except (GeneratorExit, StopAsyncIteration, ConnectionError, BrokenPipeError, OSError) as e:
                # 浏览器取消请求、关闭页面或网络断开时，yield 可能失败。
                logger.info(f"⚠️ 客户端断开连接: {type(e).__name__}: {str(e)}")
                # 重新抛给外层连接异常分支，统一结束异步生成器。
                raise
            except Exception as e:
                # 其他发送异常同样交给外层，避免流处于不明确的半完成状态。
                logger.warning(f"⚠️ 发送事件时出错: {type(e).__name__}: {str(e)}")
                raise

    except (GeneratorExit, StopAsyncIteration, ConnectionError, BrokenPipeError, OSError) as e:
        # 客户端已经无法接收数据，连接中止属于正常生命周期，不发送 error 或 DONE。
        logger.info(f"ℹ️ 客户端断开连接，停止流式响应: {type(e).__name__}")
        return
    except Exception as e:
        # 模型初始化、Prompt 组装、Agent 执行或工具调用等错误会进入这里。
        import traceback
        logger.error(f"❌ 处理聊天流时出错: {str(e)}")
        # traceback 包含完整调用栈，便于从日志定位真正的失败位置。
        logger.error(traceback.format_exc())
        try:
            # 如果连接仍可用，把内部异常转换成前端统一处理的 error 事件。
            error_event = {
                "type": "error",
                "error": str(e)
            }
            # json.dumps 生成事件正文，外层 data: 和两个换行符组成 SSE 数据块。
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            # 即使发生错误，也发送完成标记，帮助前端退出加载状态。
            yield "data: [DONE]\n\n"
        except:
            # 若错误事件本身也发送失败，说明连接大概率已经断开，只能结束生成器。
            pass

