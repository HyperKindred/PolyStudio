# PolyStudio 多模态 Agent 源码阅读顺序

这份清单只关注多模态 Agent 开发，不要求了解首页、项目管理、Excalidraw 画布、主题和设置页面。

## 核心主线

阅读时始终围绕下面这条调用链：

```text
用户消息
  -> FastAPI 接收请求
  -> 创建 LangGraph ReAct Agent
  -> 组装 LLM、Prompt、Tools
  -> LLM 决定直接回复或调用工具
  -> 图片/视频/音频/3D 工具执行
  -> 工具结果返回 Agent
  -> Agent 继续推理并生成回复
  -> StreamProcessor 输出流式事件
```

## 1. 先看 Agent 总装配点

文件：[`backend/app/services/agent_service.py`](backend/app/services/agent_service.py)

按顺序看：

1. 文件顶部导入的各种 Tool。
2. `create_agent()` 中的 `model = create_llm()`。
3. `tools = [...]` 工具注册列表。
4. `skill_service.get_skills_context()`。
5. `workspace_service.get_workspace_context()`。
6. `get_full_prompt(...)`。
7. `create_react_agent(...)`。
8. `process_chat_stream(...)`。

看完后应先形成这个结构：

```text
Agent = LLM + Prompt + Tools + Skills Context + Workspace Context
```

第一遍不用进入每个 Tool 的内部实现，只需要知道 Agent 当前具有哪些能力。

## 2. 看 LLM 是如何接入的

阅读顺序：

1. [`backend/app/llm/base.py`](backend/app/llm/base.py)
2. [`backend/app/llm/factory.py`](backend/app/llm/factory.py)
3. [`backend/app/llm/volcano.py`](backend/app/llm/volcano.py)
4. [`backend/app/llm/siliconflow.py`](backend/app/llm/siliconflow.py)

重点定位：

- `BaseLLMProvider` 定义了什么统一接口。
- `create_llm()` 如何读取 `LLM_PROVIDER`。
- 不同 Provider 如何创建 LangChain 兼容的 `BaseChatModel`。
- 模型名称、API Key、Base URL 和流式输出参数在哪里配置。
- 为什么上层 Agent 不需要知道具体使用哪家模型服务。

这里体现的是 Provider 抽象和工厂模式：

```text
AgentService -> create_llm() -> 某个 Provider -> BaseChatModel
```

## 3. 看 Agent Prompt 如何组装

文件：[`backend/app/services/prompt.py`](backend/app/services/prompt.py)

建议先从文件底部的 `get_full_prompt(...)` 开始，再反向查看它引用的各段 Prompt。

重点看：

1. 基础身份和回复规则。
2. 工具列表如何插入 Prompt。
3. 图片、视频、3D、音频等工具的调用规则。
4. Skill 上下文插入位置。
5. Workspace 身份和记忆插入位置。
6. Tool 调用前后，模型被要求如何与用户沟通。

不要逐字背 Prompt，重点理解它如何约束 Agent 的：

```text
能力边界、工具选择、参数生成、执行顺序、最终回复格式
```

## 4. 精读一个最典型的 Tool

优先阅读图片生成：

文件：[`backend/app/tools/volcano_image_generation.py`](backend/app/tools/volcano_image_generation.py)

按顺序定位：

1. `GenerateVolcanoImageInput`：工具参数模型。
2. `@tool("generate_volcano_image", args_schema=...)`：注册为 LangChain Tool。
3. `generate_volcano_image_tool(...)`：Agent 实际调用入口。
4. 外部图片 API 请求。
5. `download_and_save_image(...)`：保存生成结果。
6. 返回给 Agent 的 JSON 字符串。
7. `EditVolcanoImageInput` 和 `edit_volcano_image_tool(...)`：图片编辑的差异。

重点理解一个 Tool 的标准结构：

```text
Pydantic 参数模型
  -> @tool 名称和描述
  -> 参数预处理
  -> 调用外部模型 API
  -> 保存媒体结果
  -> 返回结构化 JSON
```

`@tool` 的函数名、描述、参数字段和字段说明都会影响 LLM 是否会调用它，以及能否生成正确参数。

## 5. 看聊天请求如何进入 Agent

文件：[`backend/app/routers/chat.py`](backend/app/routers/chat.py)

只需要看：

1. `ChatRequest`。
2. `@router.post("/chat")`。
3. 历史消息与本轮用户消息的拼接。
4. `process_chat_stream(messages, request.session_id)`。
5. `StreamingResponse`。

图片、音频、视频上传接口和画布历史保存可以先跳过。

这一层只负责把 HTTP 请求转换为 Agent 可以处理的消息：

```text
POST /api/chat JSON
  -> ChatRequest
  -> messages
  -> process_chat_stream(...)
```

## 6. 看 LangGraph 输出如何变成流式事件

文件：[`backend/app/services/stream_processor.py`](backend/app/services/stream_processor.py)

按顺序看：

1. `StreamProcessor.__init__()` 中保存的流状态。
2. `process_stream(...)`。
3. `agent.astream(...)`。
4. `_handle_chunk(...)`。
5. `_handle_message_chunk(...)`。
6. `AIMessageChunk` 的文本和工具调用处理。
7. `ToolMessage` 的工具结果处理。
8. 工具参数分片的累积逻辑。

重点掌握这些事件：

| 事件 | 含义 |
| --- | --- |
| `delta` | LLM 生成的一小段文本 |
| `tool_call` | LLM 决定调用某个工具及其参数 |
| `tool_result` | 工具执行后返回的结果 |
| `skill_matched` | Agent 判断应加载某个 Skill |
| `error` | Agent、模型或工具执行异常 |
| `[DONE]` | 本轮流式响应结束 |

这部分代码较复杂。第一遍只追踪一种文本 `delta` 和一次完整的 `tool_call -> tool_result`，之后再看参数分片兼容逻辑。

## 7. 看前端如何消费 Agent 事件

文件：[`frontend/src/components/ChatInterface.tsx`](frontend/src/components/ChatInterface.tsx)

只搜索并阅读：

1. `sendMessage`。
2. `fetch('/api/chat')`。
3. `response.body?.getReader()`。
4. `reader.read()`。
5. `switch (event.type)`。
6. `delta`、`tool_call`、`tool_result`、`error` 分支。

不需要阅读画布管理、项目列表和 Excalidraw 代码。

这一小段前端代码的价值是帮助理解 Agent API 对外暴露的事件协议，而不是学习 React 页面开发。

## 8. 看真正的多模态理解 Tool

文件：[`backend/app/tools/qwen_omni_understanding.py`](backend/app/tools/qwen_omni_understanding.py)

按顺序定位：

1. `QwenOmniUnderstandInput`。
2. `qwen_omni_understand_tool(...)`。
3. `_resolve_local_path(...)`。
4. `_detect_media_type(...)` 和 `_get_mime(...)`。
5. `_encode_file_to_base64(...)`。
6. `_call_qwen_omni(...)`。
7. 文本和音频结果如何返回。

重点理解：

```text
本地路径或 URL
  -> 判断图片/音频/视频类型
  -> 转换成模型 API 需要的输入格式
  -> 调用多模态模型
  -> 返回文本理解结果或音频结果
```

这里的“多模态理解”和图片生成不同：前者让模型理解已有媒体，后者根据指令创造新媒体。

## 9. 按模态扩展阅读其他 Tool

在看懂一个图片 Tool 后，按兴趣选择，不需要全部阅读。

### 图片生成与编辑

- [`backend/app/tools/volcano_image_generation.py`](backend/app/tools/volcano_image_generation.py)

### 视频生成

- [`backend/app/tools/volcano_video_generation.py`](backend/app/tools/volcano_video_generation.py)

重点看任务提交、轮询任务状态、结果下载，以及文本生视频、图片生视频的参数差异。

### 3D 模型生成

- [`backend/app/tools/model_3d_generation.py`](backend/app/tools/model_3d_generation.py)

重点看文本/图片输入、异步任务轮询、压缩包解压，以及 OBJ/GLB 结果组织。

### TTS 和声音克隆

- [`backend/app/tools/qwen_tts.py`](backend/app/tools/qwen_tts.py)

重点看 `VoiceDesignInput`、`VoiceCloningInput`、音频输入准备和 Base64 音频保存。

## 10. 看多工具工作流如何组合

单个 Tool 看懂后，再看需要多步协作的能力。

推荐顺序：

1. [`backend/app/tools/video_concatenation.py`](backend/app/tools/video_concatenation.py)
2. [`backend/app/tools/audio_mixing.py`](backend/app/tools/audio_mixing.py)
3. [`backend/app/tools/virtual_anchor_generation.py`](backend/app/tools/virtual_anchor_generation.py)

这些文件体现的不是新 Agent 框架，而是 Agent 如何串联多个原子工具：

```text
生成多个视频 -> 视频拼接
生成多段语音 -> 音频拼接 -> 选择 BGM -> 混音
检测人脸 -> 准备图片和音频 -> 调用虚拟人工作流
```

阅读时重点关注每个工具的输入输出是否能被下一个工具直接使用。

## 11. 看 Skill 渐进加载

阅读顺序：

1. [`backend/app/services/skill_service.py`](backend/app/services/skill_service.py)
2. [`backend/app/tools/skill_tools.py`](backend/app/tools/skill_tools.py)
3. 任意一个 `backend/skills/custom/*/SKILL.md`
4. 回到 [`backend/app/services/agent_service.py`](backend/app/services/agent_service.py) 看 Skill Context 注入位置。

重点追踪：

```text
扫描 SKILL.md 元数据
  -> 只把名称、描述和路径加入 Prompt
  -> LLM 判断是否匹配用户需求
  -> 调用 read_skill_file
  -> 加载完整工作流
  -> 按 Skill 编排多个 Tool
```

Skill 解决的是复杂任务工作流和领域指令扩展，不是新增底层模型能力。

## 12. 最后看 Agent 身份和长期记忆

阅读顺序：

1. [`backend/app/services/workspace_service.py`](backend/app/services/workspace_service.py)
2. [`backend/app/tools/workspace_tools.py`](backend/app/tools/workspace_tools.py)
3. `backend/workspace/AGENTS.md`
4. `backend/workspace/IDENTITY.md`
5. `backend/workspace/USER.md`
6. `backend/workspace/SOUL.md`
7. `backend/workspace/MEMORY.md`
8. 回到 [`backend/app/services/agent_service.py`](backend/app/services/agent_service.py) 看 Workspace Context 注入位置。

重点理解两条路径：

```text
Workspace 文件 -> get_workspace_context() -> System Prompt
Agent 调用 write_memory -> 更新 MEMORY.md -> 后续 Agent 读取
```

这部分实现的是跨会话身份、偏好和长期记忆，与单轮 `messages` 聊天上下文不同。

## 最短阅读路径

如果只想尽快看懂核心 Agent，按下面 8 个入口即可：

1. `agent_service.py` 的 `create_agent()`。
2. `llm/factory.py` 的 `create_llm()`。
3. `prompt.py` 的 `get_full_prompt()`。
4. `volcano_image_generation.py` 的参数模型、`@tool` 和工具函数。
5. `chat.py` 的 `POST /chat`。
6. `stream_processor.py` 的 `process_stream()`。
7. `qwen_omni_understanding.py` 的 Tool 入口。
8. `skill_service.py` 的 `get_skills_context()`。

## 可以跳过的代码

如果目标只是学习多模态 Agent，以下内容暂时不需要看：

- `frontend/src/App.tsx`
- `frontend/src/components/HomePage.tsx`
- `frontend/src/components/ExcalidrawCanvas.tsx`
- `frontend/src/components/SettingsPage.tsx`
- `frontend/src/components/Model3DViewer.tsx`
- `backend/app/services/history_service.py`
- `chat.py` 中的画布 CRUD 和文件上传接口
- `main.py` 中的静态文件挂载和页面相关配置
- `ChatInterface.tsx` 中除聊天请求和 SSE 解析之外的部分

