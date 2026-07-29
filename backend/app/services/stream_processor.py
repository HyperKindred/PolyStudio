"""将 LangGraph 流式输出转换为前端约定的 SSE 事件。

LangGraph 在执行 ReAct Agent 时会产生多种 Python 对象，例如：

- ``AIMessageChunk``：LLM 的增量文本或 Tool Call；
- ``ToolMessage``：工具执行完成后的返回结果；
- 完整状态或不同版本产生的 tuple/list 包装结构。

本模块把这些内部对象统一转换为 ``delta``、``tool_call``、``tool_result``、
``skill_matched``、``error`` 和 ``[DONE]`` 等文本事件，使 HTTP 路由和前端无需
理解 LangGraph 的具体消息类型。
"""
import json
import logging
import os
from typing import List, Dict, Any, AsyncGenerator, Optional
from langchain_core.messages import (
    AIMessageChunk, 
    ToolMessage, 
    HumanMessage,
    AIMessage,
    convert_to_openai_messages,
    ToolCall
)

# 日志格式由 main.py 统一配置，这里只取得当前模块的命名 logger。
logger = logging.getLogger(__name__)


class StreamProcessor:
    """维护一轮 Agent 流的解析状态，并异步产出 SSE 字符串。"""

    def __init__(self, session_id: Optional[str] = None):
        # session_id 当前主要作为本轮处理器的上下文标识保留。
        self.session_id = session_id
        # 这两个字段为流内容/工具调用状态预留；当前主要逻辑使用下面的专用缓冲区。
        self.current_content = ""
        self.current_tool_calls = []
        # 仅用于把零碎文本合并后打印日志，不影响发送给前端的逐块 delta。
        self.text_buffer = ""
        # Tool Call 参数可能跨多个模型 chunk 到达，按调用 ID 保存已解析的参数。
        self.tool_call_args: Dict[str, Dict[str, Any]] = {}
        # 参数分片常常不重复携带工具名，因此单独保存 tool_call_id -> tool_name 映射。
        self.tool_call_names: Dict[str, str] = {}
        # 字符串形式的 JSON 参数可能被拆成 '{"prompt":' 和 '"..."}'，先在这里拼接。
        self.tool_call_args_buffer: Dict[str, str] = {}
        # 同一次 read_skill_file 调用只向前端发送一次 Skill 命中提示。
        self.skill_matched_emitted: set = set()
        # recursion_limit 限制 LangGraph 节点执行步数，不是 Python 函数递归深度。
        # 多工具工作流会反复经历“模型 -> 工具 -> 模型”，默认 25 步可能不足。
        self.recursion_limit = int(os.getenv("RECURSION_LIMIT", "200"))

    def _extract_skill_name(self, path: str) -> str:
        """从 SKILL.md 路径中提取 Skill 目录名，供 UI 显示命中状态。"""

        # Windows 和 Unix 路径统一转成 / 后再按目录段处理。
        parts = path.replace("\\", "/").split("/")
        # 路径格式: custom/<skill-name>/SKILL.md 或 public/<skill-name>/...
        for i, part in enumerate(parts):
            if part in ("custom", "public", "builtin") and i + 1 < len(parts):
                return parts[i + 1]
        # fallback：取 .md 文件的父目录名
        for i, part in enumerate(parts):
            if part.endswith(".md") and i > 0:
                return parts[i - 1]
        # 最终 fallback：倒数第二段
        return parts[-2] if len(parts) >= 2 else ""

    def _extract_skill_display_name(self, skill_id: str) -> str:
        """把目录 ID 转换为 SKILL.md frontmatter 中更适合展示的 name。"""
        try:
            # 放在函数内部导入，避免模块加载阶段产生不必要的循环依赖。
            from app.services import skill_service
            skills = skill_service.get_skills_with_state()
            for s in skills:
                if s.id == skill_id:
                    return s.name
        except Exception:
            # Skill 扫描失败不应中断 Agent 主流；退化为展示目录 ID。
            pass
        return skill_id

    async def _maybe_emit_skill_matched(self, tool_name: str, tool_call_id: str, tool_args: dict) -> AsyncGenerator[str, None]:
        """在 Agent 首次读取某个 Skill 时产生一次 ``skill_matched`` 事件。"""

        # “LLM 调用 read_skill_file”是确认真正选择了 Skill 的可靠信号；仅看用户文本不够。
        if tool_name == "read_skill_file" and tool_call_id not in self.skill_matched_emitted:
            skill_path = tool_args.get("path", "")
            skill_id = self._extract_skill_name(skill_path)
            if skill_id:
                skill_display_name = self._extract_skill_display_name(skill_id)
                logger.info(f"🎯 命中 skill: {skill_id} ({skill_display_name})")
                skill_event = {
                    "type": "skill_matched",
                    "skill_name": skill_display_name,
                    "tool_call_id": tool_call_id
                }
                self.skill_matched_emitted.add(tool_call_id)
                # SSE 每条 data 事件以两个换行符结束，前端据此识别事件边界。
                yield f"data: {json.dumps(skill_event, ensure_ascii=False)}\n\n"

    async def process_stream(
        self,
        agent: Any,
        messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[str, None]:
        """运行 Agent，规范化每个输出 chunk，并逐个产出 SSE 事件。

        Args:
            agent: ``create_react_agent`` 返回的已编译 LangGraph。
            messages: 路由传入的 OpenAI 风格 ``role/content`` 字典列表。

        Yields:
            可以直接交给 FastAPI ``StreamingResponse`` 的 SSE 文本块。
        """
        try:
            logger.info(f"🚀 开始处理流式响应，消息数量: {len(messages)}")
            
            # LangGraph State 中的 messages 使用 LangChain 消息对象，而路由收到的是普通字典。
            langchain_messages = []
            for msg in messages:
                if msg.get("role") == "user":
                    langchain_messages.append(HumanMessage(content=msg.get("content", "")))
                elif msg.get("role") == "assistant":
                    langchain_messages.append(AIMessage(content=msg.get("content", "")))
                # 其他 role 当前不会传入图，Tool 历史由本轮 LangGraph 执行时自行产生。
            
            logger.info(f"📨 转换后的消息数量: {len(langchain_messages)}")
            
            # astream 真正启动编译后的图，并在模型或工具产生新消息时异步交出 chunk。
            # messages 模式关注消息增量，适合将 LLM 文本尽快转发给前端。
            chunk_count = 0
            try:
                async for chunk in agent.astream(
                    # 图的初始 State；create_react_agent 默认使用 messages 状态键。
                    {"messages": langchain_messages},
                    # 第二个参数是 LangGraph 本轮运行配置。
                    {"recursion_limit": self.recursion_limit},
                    # 使用列表形式指定模式时，不同 LangGraph 版本可能增加模式包装层，
                    # 后面的 _handle_chunk 会兼容 tuple、list 和直接消息对象。
                    stream_mode=["messages"]
                ):
                    chunk_count += 1
                    logger.debug(f"📦 收到第 {chunk_count} 个 chunk: {type(chunk)}")
                    # 一个 LangGraph chunk 可能转换为零个、一个或多个前端事件。
                    event_count = 0
                    try:
                        async for event in self._handle_chunk(chunk):
                            event_count += 1
                            logger.debug(f"📤 发送第 {event_count} 个事件 (chunk {chunk_count}): {event[:100] if len(event) > 100 else event}")
                            # 每转换出一个事件就立即向 agent_service 交出，不等待图执行结束。
                            yield event
                    except (GeneratorExit, StopAsyncIteration, ConnectionError, BrokenPipeError, OSError) as e:
                        # 客户端断开，停止处理
                        logger.info(f"⚠️ 客户端断开连接，停止处理 chunk: {type(e).__name__}")
                        raise  # 重新抛出，让上层处理
                    logger.debug(f"✅ Chunk {chunk_count} 处理完成，发送了 {event_count} 个事件")
            except (GeneratorExit, StopAsyncIteration, ConnectionError, BrokenPipeError, OSError) as e:
                # 客户端断开连接，停止 agent 处理
                logger.info(f"ℹ️ 客户端断开连接，停止 agent 流式处理: {type(e).__name__}")
                # 不继续处理，直接返回
                return

            # 图自然结束后，先补打一段尚未达到日志分段阈值的文本。
            if self.text_buffer:
                logger.info(f"🤖 AI回答(完): {self.text_buffer}")
                self.text_buffer = ""
            
            logger.info("✅ 流式处理完成")
            # [DONE] 是本项目约定的 SSE 流结束标记，不是 LangGraph 消息对象。
            yield "data: [DONE]\n\n"

        except Exception as e:
            # 图运行、消息转换或事件序列化失败时，尽量把统一 error 事件交给前端。
            import traceback
            logger.error(f"❌ 流式处理错误: {str(e)}")
            logger.error(traceback.format_exc())
            error_event = {
                "type": "error",
                "error": str(e),
                "traceback": traceback.format_exc()
            }
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    async def _handle_chunk(self, chunk: Any) -> AsyncGenerator[str, None]:
        """把不同包装形式的 LangGraph chunk 统一分派到具体消息处理函数。"""
        try:
            logger.debug(f"🔍 处理 chunk: type={type(chunk)}, value={str(chunk)[:200]}")
            
            # 兼容形式一：二元组，第一项是流模式/类型，第二项是对应数据。
            if isinstance(chunk, tuple) and len(chunk) == 2:
                chunk_type = chunk[0]
                chunk_data = chunk[1]
                logger.debug(f"  📦 Tuple chunk: type={chunk_type}, data_type={type(chunk_data)}")
                
                if chunk_type == "values":
                    # values 模式携带的是完整 State，而不是单个增量消息。
                    async for event in self._handle_values_chunk(chunk_data):
                        yield event
                else:
                    # 处理消息流
                    if isinstance(chunk_data, list) and len(chunk_data) > 0:
                        logger.debug(f"  📋 消息列表，长度: {len(chunk_data)}")
                        for message in chunk_data:
                            async for event in self._handle_message_chunk(message):
                                yield event
                    elif hasattr(chunk_data, '__iter__') and not isinstance(chunk_data, str):
                        # 可迭代对象
                        logger.debug(f"  🔄 可迭代对象")
                        for message in chunk_data:
                            async for event in self._handle_message_chunk(message):
                                yield event
                    else:
                        # 单个消息对象
                        logger.debug(f"  📨 单个消息对象")
                        async for event in self._handle_message_chunk(chunk_data):
                            yield event
            # 兼容形式二：直接返回消息列表。
            elif isinstance(chunk, list) and len(chunk) > 0:
                logger.debug(f"  📋 直接列表格式，长度: {len(chunk)}")
                for message in chunk:
                    async for event in self._handle_message_chunk(message):
                        yield event
            # 兼容形式三：直接返回 AIMessageChunk、ToolMessage 等单个对象。
            else:
                logger.debug(f"  📨 直接消息对象")
                async for event in self._handle_message_chunk(chunk):
                    yield event
        except Exception as e:
            import traceback
            logger.error(f"❌ 处理 chunk 时出错: {str(e)}")
            logger.error(traceback.format_exc())
            error_event = {
                "type": "error",
                "error": f"处理chunk时出错: {str(e)}"
            }
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    async def _handle_values_chunk(self, chunk_data: Dict[str, Any]) -> AsyncGenerator[str, None]:
        """把 values 模式携带的完整 LangGraph 消息状态转成一个 messages 事件。"""
        all_messages = chunk_data.get("messages", [])
        
        if all_messages:
            # LangChain 消息对象不能直接 JSON 序列化，先转为 OpenAI 风格字典。
            oai_messages = convert_to_openai_messages(all_messages)
            
            # 发送完整消息更新
            event = {
                "type": "messages",
                "messages": oai_messages
            }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def _handle_message_chunk(self, message_chunk: Any) -> AsyncGenerator[str, None]:
        """将一个 LangChain 消息对象转换成文本、工具调用或工具结果事件。"""
        try:
            # ToolMessage 表示 ToolNode 已执行完某次调用，content 是 Tool 返回值。
            if isinstance(message_chunk, ToolMessage):
                logger.info(f"🔧 工具调用结果: tool_call_id={message_chunk.tool_call_id}")
                logger.info(f"   内容: {str(message_chunk.content)[:200]}")
                # 工具已经完成，释放该 ID 的解析状态，避免影响之后的工具调用。
                if message_chunk.tool_call_id in self.tool_call_args:
                    del self.tool_call_args[message_chunk.tool_call_id]
                if message_chunk.tool_call_id in self.tool_call_names:
                    del self.tool_call_names[message_chunk.tool_call_id]
                event = {
                    "type": "tool_result",
                    "tool_call_id": message_chunk.tool_call_id,
                    "content": message_chunk.content
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                # ToolMessage 已处理完成，不应继续按 AIMessageChunk 分支判断。
                return

            # AIMessageChunk 既可能含增量自然语言，也可能含模型生成的 Tool Call。
            if isinstance(message_chunk, AIMessageChunk):
                logger.debug(f"  🤖 AIMessageChunk: content={str(message_chunk.content)[:50] if message_chunk.content else None}")
                # AIMessageChunk.content 本身就是增量，不要再次与历史文本做差分。
                content = message_chunk.content
                
                # 如果 content 存在，立即发送（每个 chunk 都是增量）
                if content is not None and content != "":
                    content_str = str(content) if not isinstance(content, str) else content
                    
                    # 直接发送这个 chunk 的内容（langgraph 已经处理了增量）
                    # 类似 OpenAI: chunk.choices[0].delta.content
                    if content_str:
                        logger.debug(f"📝 发送文本 delta ({len(content_str)} 字符): {content_str[:100]}")
                        
                        # 日志缓冲只减少零碎日志数量，前端仍会收到每一个原始 delta。
                        self.text_buffer += content_str
                        # 如果遇到换行符或标点符号，且长度足够，则打印
                        if "\n" in self.text_buffer or (len(self.text_buffer) > 50 and any(p in self.text_buffer for p in "。！？.!?")):
                            # 移除换行符，保持日志整洁
                            log_content = self.text_buffer.replace("\n", " ")
                            if log_content.strip():
                                logger.info(f"🤖 AI回答: {log_content}")
                            self.text_buffer = ""
                            
                        event = {
                            "type": "delta",
                            "content": content_str
                        }
                        # 将 Python 字典序列化成 SSE data 行。
                        event_str = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        logger.debug(f"📤 发送事件字符串: {event_str[:100]}")
                        yield event_str
                else:
                    logger.debug(f"  ⚠️  AIMessageChunk 没有内容")

                # tool_calls 通常是已经聚合出的调用对象，但不同模型适配器返回格式不同。
                if hasattr(message_chunk, "tool_calls") and message_chunk.tool_calls:
                    for tool_call in message_chunk.tool_calls:
                        # 处理不同的工具调用格式
                        if isinstance(tool_call, dict):
                            tool_call_id = tool_call.get("id")
                            tool_name = tool_call.get("name")
                            # 尝试多种可能的参数字段名
                            tool_args = tool_call.get("args") or tool_call.get("arguments") or {}
                            logger.debug(f"📋 字典格式工具调用: id={tool_call_id}, name={tool_name}, args={tool_args}, args类型={type(tool_args)}")
                        else:
                            # ToolCall 对象
                            tool_call_id = getattr(tool_call, "id", None)
                            tool_name = getattr(tool_call, "name", None)
                            # 尝试多种可能的参数属性名
                            tool_args = getattr(tool_call, "args", None) or getattr(tool_call, "arguments", None)
                            if tool_args is None:
                                # 尝试通过 dict() 方法获取
                                if hasattr(tool_call, "dict"):
                                    tool_dict = tool_call.dict()
                                    tool_args = tool_dict.get("args") or tool_dict.get("arguments") or {}
                                    logger.debug(f"📋 通过dict()获取参数: {tool_args}")
                                else:
                                    tool_args = {}
                            logger.debug(f"📋 对象格式工具调用: id={tool_call_id}, name={tool_name}, args={tool_args}, args类型={type(tool_args)}, 对象类型={type(tool_call)}")
                        
                        # 流式 Tool Call 的中间 chunk 可能尚未携带 name 或 id，暂时跳过。
                        if not tool_name or not tool_call_id:
                            logger.debug(f"⚠️  跳过无效的工具调用 (name或id为空): name={tool_name}, id={tool_call_id}")
                            continue
                        
                        # 后续 tool_call_chunks 可能只有参数，没有名称，需要通过 ID 找回。
                        if tool_name:
                            self.tool_call_names[tool_call_id] = tool_name

                        # 有的 Provider 返回 dict，有的返回 JSON 字符串，这里统一为 dict。
                        if isinstance(tool_args, str):
                            try:
                                tool_args = json.loads(tool_args)
                                logger.debug(f"✅ 成功解析JSON参数: {tool_args}")
                            except json.JSONDecodeError as e:
                                logger.warning(f"⚠️  工具参数不是有效的JSON，使用空字典: {tool_args}, 错误: {e}")
                                tool_args = {}
                        elif tool_args is None:
                            tool_args = {}
                            logger.debug(f"⚠️  工具参数为None，使用空字典")

                        # 为本次调用建立参数字典；后续同 ID 的参数会合并进来。
                        if tool_call_id not in self.tool_call_args:
                            self.tool_call_args[tool_call_id] = {}
                        
                        # 合并参数（后续chunk可能包含更多参数）
                        if tool_args and isinstance(tool_args, dict):
                            self.tool_call_args[tool_call_id].update(tool_args)
                            logger.debug(f"✅ 从tool_calls累积参数: id={tool_call_id}, 新参数={tool_args}, 累积后={self.tool_call_args[tool_call_id]}")
                        
                        # 使用累积的参数
                        final_args = self.tool_call_args[tool_call_id]

                        # 空参数常表示调用尚未流完，此时先等待，避免前端显示无效调用。
                        if final_args:
                            logger.info(f"🛠️  工具调用: name={tool_name}, id={tool_call_id}")
                            logger.info(f"   参数: {final_args}")

                            # Skill UI 事件必须先于 read_skill_file 的普通 tool_call 事件。
                            async for ev in self._maybe_emit_skill_matched(tool_name, tool_call_id, final_args):
                                yield ev

                            event = {
                                "type": "tool_call",
                                "id": tool_call_id,
                                "name": tool_name,
                                "arguments": final_args
                            }
                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        else:
                            logger.debug(f"⏳ 工具调用参数为空，等待后续chunk补充: name={tool_name}, id={tool_call_id}")
                
                # tool_call_chunks 是更底层的参数增量，例如 JSON 字符串的一小段。
                if hasattr(message_chunk, "tool_call_chunks") and message_chunk.tool_call_chunks:
                    for tool_call_chunk in message_chunk.tool_call_chunks:
                        # 处理可能的字典或对象
                        chunk_dict = tool_call_chunk
                        if not isinstance(chunk_dict, dict):
                            # 尝试转为字典
                            if hasattr(tool_call_chunk, "dict"):
                                chunk_dict = tool_call_chunk.dict()
                            else:
                                chunk_dict = {"args": str(tool_call_chunk)} # fallback

                        # 提取信息
                        args_chunk = chunk_dict.get("args")
                        index = chunk_dict.get("index", 0)
                        tc_id = chunk_dict.get("id")
                        tool_name_from_chunk = chunk_dict.get("name")
                        
                        # 某些 Provider 只在首个分片发送 ID；缺失时退化为最近记录的调用。
                        if not tc_id:
                            # 尝试从已存储的 tool_call_names 中获取（如果有多个，使用最后一个）
                            if self.tool_call_names:
                                # 使用最近添加的 tool_call_id（假设 index=0 对应最新的）
                                tc_id = list(self.tool_call_names.keys())[-1] if self.tool_call_names else None
                                logger.debug(f"⚠️  chunk中没有id，使用最近的tool_call_id: {tc_id}")
                        
                        # 只有同时存在参数内容和可关联的调用 ID 才能继续处理。
                        if args_chunk and tc_id:
                            # 初始化缓冲区
                            if tc_id not in self.tool_call_args_buffer:
                                self.tool_call_args_buffer[tc_id] = ""
                            
                            # 字符串参数要一直拼到成为合法完整 JSON 后才能通知前端。
                            if isinstance(args_chunk, str):
                                self.tool_call_args_buffer[tc_id] += args_chunk
                                
                                # 尝试解析累积的JSON字符串
                                try:
                                    parsed_args = json.loads(self.tool_call_args_buffer[tc_id])
                                    if isinstance(parsed_args, dict):
                                        # 解析成功，更新参数
                                        if tc_id not in self.tool_call_args:
                                            self.tool_call_args[tc_id] = {}
                                        self.tool_call_args[tc_id].update(parsed_args)
                                        
                                        # 查找工具名称
                                        tool_name_from_storage = self.tool_call_names.get(tc_id)
                                        tool_name = tool_name_from_storage or tool_name_from_chunk
                                        
                                        if tool_name:
                                            logger.info(f"🛠️  工具调用（参数更新）: name={tool_name}, id={tc_id}")
                                            logger.info(f"   参数: {self.tool_call_args[tc_id]}")

                                            # 如果是 read_skill_file，先发送 skill_matched 事件
                                            async for ev in self._maybe_emit_skill_matched(tool_name, tc_id, self.tool_call_args[tc_id]):
                                                yield ev

                                            # 发送更新后的工具调用事件（包含完整参数）
                                            event = {
                                                "type": "tool_call",
                                                "id": tc_id,
                                                "name": tool_name,
                                                "arguments": self.tool_call_args[tc_id]
                                            }
                                            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                                except json.JSONDecodeError:
                                    # 这通常不是错误，只表示后续 chunk 尚未到达。
                                    pass
                            elif isinstance(args_chunk, dict):
                                # 已经结构化的参数无需字符串缓冲，直接按键合并。
                                if tc_id not in self.tool_call_args:
                                    self.tool_call_args[tc_id] = {}
                                self.tool_call_args[tc_id].update(args_chunk)
                                
                                tool_name_from_storage = self.tool_call_names.get(tc_id)
                                tool_name = tool_name_from_storage or tool_name_from_chunk
                                
                                if tool_name:
                                    logger.info(f"🛠️  工具调用（参数更新）: name={tool_name}, id={tc_id}")
                                    logger.info(f"   参数: {self.tool_call_args[tc_id]}")

                                    # 如果是 read_skill_file，先发送 skill_matched 事件
                                    async for ev in self._maybe_emit_skill_matched(tool_name, tc_id, self.tool_call_args[tc_id]):
                                        yield ev

                                    event = {
                                        "type": "tool_call",
                                        "id": tc_id,
                                        "name": tool_name,
                                        "arguments": self.tool_call_args[tc_id]
                                    }
                                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        
                        # 项目当前只展示完整参数，因此原始 tool_call_chunk 事件保持关闭。
                        # if args_chunk:
                        #     event = {
                        #         "type": "tool_call_chunk",
                        #         "index": index,
                        #         "id": tc_id,
                        #         "args": args_chunk
                        #     }
                        #     yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        except Exception as e:
            logger.error(f"❌ 处理消息chunk时出错: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            error_event = {
                "type": "error",
                "error": f"处理消息chunk时出错: {str(e)}"
            }
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
