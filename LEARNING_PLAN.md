# PolyStudio 代码学习计划

> 建议周期：10 个学习日  
> 建议投入：每天 1.5～2.5 小时  
> 默认基础：了解 Python、TypeScript、React 基础语法  
> 学习方法：先跑通功能，再追调用链；先理解主干，再阅读具体媒体工具

## 一、学习目标

完成本计划后，应能够：

- [ ] 说明 PolyStudio 前端、后端、Agent、工具和画布之间的关系
- [ ] 独立追踪一次聊天请求从前端到后端再返回画布的完整过程
- [ ] 理解 LangGraph ReAct Agent 如何选择并调用工具
- [ ] 理解 SSE 与 WebSocket 在项目中的不同用途
- [ ] 理解项目、聊天记录和 Excalidraw 数据如何持久化
- [ ] 理解 Skill 渐进加载与 Workspace 长期记忆机制
- [ ] 能够添加一个简单工具、接口或前端事件处理逻辑

## 二、项目核心链路

学习期间始终围绕下面这条主线定位代码：

```text
用户输入
  ↓
React / ChatInterface
  ↓ POST /api/chat
FastAPI / chat router
  ↓
AgentService / LangGraph ReAct Agent
  ↓
LLM 判断是否调用 Tool
  ↓
StreamProcessor 转换为 SSE 事件
  ↓
ChatInterface 解析 delta / tool_call / tool_result
  ↓
ExcalidrawCanvas 插入图片、视频或 3D 模型
```

## 三、开始前准备

### 基础知识检查

不熟悉的部分只需先学习基本概念，不必系统学完框架：

- Python：异步生成器、`async` / `await`、类型标注
- FastAPI：路由、Pydantic、`StreamingResponse`、WebSocket
- React：组件、Props、`useState`、`useEffect`、`useRef`
- TypeScript：接口、联合类型、可选字段
- HTTP：JSON 请求、SSE、WebSocket
- Agent：Prompt、Tool Calling、ReAct 基本循环

### 建议准备的学习工具

- 浏览器开发者工具：重点使用 Network 和 Console
- FastAPI Swagger：`http://localhost:8000/docs`
- 后端日志：观察 Agent 创建、工具调用和异常
- 一张自己的调用链笔记图

## 四、10 日学习安排

### 第 1 日：运行项目并建立全局认识

阅读：

1. [`README.md`](README.md)
2. [`frontend/package.json`](frontend/package.json)
3. [`backend/requirements.txt`](backend/requirements.txt)
4. [`frontend/vite.config.ts`](frontend/vite.config.ts)

实践：

- [ ] 启动后端并访问 `/health` 和 `/docs`
- [ ] 启动前端，进入首页、编辑器和设置页
- [ ] 在浏览器 Network 中观察 `/api/canvases`
- [ ] 找到前端 `/api` 和 `/storage` 的代理配置
- [ ] 记录项目使用的核心框架及其职责

当天产出：

```text
React + Vite：页面和交互
Excalidraw：无限画布
FastAPI：HTTP、SSE、WebSocket 接口
LangGraph：Agent 编排与工具调用
本地 JSON / storage：项目和媒体持久化
```

### 第 2 日：理解前后端入口

阅读顺序：

1. [`frontend/src/main.tsx`](frontend/src/main.tsx)
2. [`frontend/src/App.tsx`](frontend/src/App.tsx)
3. [`backend/app/main.py`](backend/app/main.py)
4. [`backend/app/routers/chat.py`](backend/app/routers/chat.py) 的路由声明部分

重点问题：

- [ ] 前端如何根据 URL 显示首页、编辑器或设置页？
- [ ] `canvasId` 和 `page` 参数分别控制什么？
- [ ] 后端如何注册 `/api` 路由？
- [ ] `/storage` 如何映射到本地文件？
- [ ] `/ws/{canvas_id}` 在什么场景下使用？

当天练习：

- 在纸上或笔记中画出“浏览器 → Vite 代理 → FastAPI”的请求路径。

### 第 3 日：项目、消息和画布数据

阅读顺序：

1. [`backend/app/services/history_service.py`](backend/app/services/history_service.py)
2. [`backend/app/routers/chat.py`](backend/app/routers/chat.py) 中的画布 CRUD
3. [`frontend/src/components/ChatInterface.tsx`](frontend/src/components/ChatInterface.tsx) 中以下逻辑：
   - `fetchCanvases`
   - `saveCanvasToBackend`
   - `createNewCanvas`
   - 当前画布切换与保存相关的 `useEffect`

需要掌握的数据结构：

```text
Canvas
├── id
├── name
├── createdAt
├── messages
└── data
    ├── elements
    ├── appState
    └── files
```

实践：

- [ ] 创建一个项目并观察 `backend/storage/chat_history.json`
- [ ] 修改画布后刷新页面，确认数据恢复过程
- [ ] 删除项目，确认前端请求和本地 JSON 的变化
- [ ] 解释旧版 `images` 字段为什么仍然存在

### 第 4 日：前端如何发送聊天请求

集中阅读 [`frontend/src/components/ChatInterface.tsx`](frontend/src/components/ChatInterface.tsx)：

- 消息相关 TypeScript 类型
- 输入内容与附件状态
- 发送消息的处理函数
- `fetch('/api/chat')`
- `AbortController`
- `response.body.getReader()`
- SSE 文本缓冲和逐行解析

重点问题：

- [ ] 前端向后端发送了哪些字段？
- [ ] 为什么要把历史消息和当前消息分开处理？
- [ ] 为什么网络数据需要先放进 `buffer`？
- [ ] 暂停生成时如何终止读取？
- [ ] `delta` 如何合并到最后一条助手消息？

当天实践：

- 在浏览器 Network 中找到 `/api/chat`，记录请求体和至少一种 SSE 事件。

### 第 5 日：后端聊天主链路

阅读顺序：

1. [`backend/app/routers/chat.py`](backend/app/routers/chat.py) 的 `chat`
2. [`backend/app/services/agent_service.py`](backend/app/services/agent_service.py) 的 `process_chat_stream`
3. [`backend/app/services/stream_processor.py`](backend/app/services/stream_processor.py) 的 `process_stream`

跟踪下面的调用关系：

```text
POST /api/chat
  → chat()
  → stream_and_save()
  → process_chat_stream()
  → create_agent()
  → StreamProcessor.process_stream()
  → agent.astream()
```

实践：

- [ ] 找出用户消息被加入历史的位置
- [ ] 找出助手文本被累计的位置
- [ ] 找出工具结果被保存的位置
- [ ] 找出流结束后项目历史被更新的位置
- [ ] 写下每个函数的输入、输出和职责

### 第 6 日：SSE 事件和流式处理

精读 [`backend/app/services/stream_processor.py`](backend/app/services/stream_processor.py)，重点关注：

- `process_stream`
- `_handle_chunk`
- `_handle_message_chunk`
- 工具参数分片的累积
- `AIMessageChunk` 与 `ToolMessage`

整理事件表：

| 事件 | 用途 | 前端处理结果 |
| --- | --- | --- |
| `delta` | 助手文本增量 | 追加到消息内容 |
| `skill_matched` | Agent 命中 Skill | 显示 Skill 标识 |
| `tool_call` | 开始调用工具 | 显示执行中的工具步骤 |
| `tool_result` | 工具执行完成 | 更新步骤并提取媒体 URL |
| `error` | 后端执行异常 | 显示错误状态 |
| `[DONE]` | 流结束 | 停止读取和加载状态 |

实践：

- [ ] 从后端的一种事件出发，定位前端对应的 `switch` 分支
- [ ] 解释工具参数为什么可能需要跨多个 chunk 拼接
- [ ] 解释 SSE 为什么适合当前聊天响应

### 第 7 日：Agent、Prompt 和模型工厂

阅读顺序：

1. [`backend/app/services/agent_service.py`](backend/app/services/agent_service.py) 的 `create_agent`
2. [`backend/app/services/prompt.py`](backend/app/services/prompt.py)
3. [`backend/app/llm/factory.py`](backend/app/llm/factory.py)
4. [`backend/app/llm/base.py`](backend/app/llm/base.py)
5. [`backend/app/llm/volcano.py`](backend/app/llm/volcano.py)
6. [`backend/app/llm/siliconflow.py`](backend/app/llm/siliconflow.py)

重点问题：

- [ ] 工具列表在哪里注册？
- [ ] 工具的名称、描述和参数如何提供给模型？
- [ ] Prompt 由哪些部分拼装而成？
- [ ] `create_react_agent` 的模型、工具和 Prompt 分别负责什么？
- [ ] `LLM_PROVIDER` 如何决定具体模型实现？

当天产出：

用自己的话描述一次 ReAct 循环：

```text
模型读取用户需求 → 判断调用工具 → 生成工具参数 →
执行工具 → 读取工具结果 → 继续调用或生成最终回复
```

### 第 8 日：追踪一个完整媒体工具

优先选择图片生成，阅读：

1. [`backend/app/tools/volcano_image_generation.py`](backend/app/tools/volcano_image_generation.py)
2. [`backend/app/services/agent_service.py`](backend/app/services/agent_service.py) 中的工具注册
3. [`backend/app/services/stream_processor.py`](backend/app/services/stream_processor.py) 中的工具结果处理
4. [`frontend/src/components/ChatInterface.tsx`](frontend/src/components/ChatInterface.tsx) 中的 `tool_result` 分支
5. [`frontend/src/components/ExcalidrawCanvas.tsx`](frontend/src/components/ExcalidrawCanvas.tsx) 中的 `addImage`

完整链路：

```text
自然语言需求
  → generate_volcano_image_tool
  → 外部图片 API
  → 保存到 storage/images
  → tool_result.image_url
  → ChatInterface
  → ExcalidrawCanvas.addImage
```

实践：

- [ ] 记录工具输入参数和返回 JSON
- [ ] 找到公网 URL、本地路径和 `/storage` URL 的转换位置
- [ ] 找到生成结果插入画布的位置
- [ ] 解释工具异常如何传回前端

完成图片链路后，再按兴趣选读一种工具：

- 视频：`backend/app/tools/volcano_video_generation.py`
- 3D：`backend/app/tools/model_3d_generation.py`
- TTS：`backend/app/tools/qwen_tts.py`
- 多模态理解：`backend/app/tools/qwen_omni_understanding.py`
- 虚拟人：`backend/app/tools/virtual_anchor_generation.py`
- 音频处理：`backend/app/tools/audio_mixing.py`

### 第 9 日：Skill 与 Workspace 记忆

阅读顺序：

1. [`backend/app/services/skill_service.py`](backend/app/services/skill_service.py)
2. [`backend/app/tools/skill_tools.py`](backend/app/tools/skill_tools.py)
3. [`backend/skills/public/skill-creator/SKILL.md`](backend/skills/public/skill-creator/SKILL.md)
4. 任意一个 `backend/skills/custom/*/SKILL.md`
5. [`backend/app/services/workspace_service.py`](backend/app/services/workspace_service.py)
6. [`backend/app/tools/workspace_tools.py`](backend/app/tools/workspace_tools.py)
7. `backend/workspace/` 下的身份和记忆文件

掌握 Skill 渐进加载流程：

```text
扫描 SKILL.md 元数据
  → 将名称和描述注入 Prompt
  → 模型判断是否匹配
  → 调用 read_skill_file
  → 加载完整工作流
  → 按 Skill 执行任务
```

实践：

- [ ] 区分 `public` 和 `custom` Skill
- [ ] 找到 Skill 启用状态的存储位置
- [ ] 解释为什么不在启动时加载所有 Skill 全文
- [ ] 区分 Workspace 身份信息与聊天历史
- [ ] 找到 Agent 写入长期记忆的工具

### 第 10 日：WebSocket、设置页和总结实践

阅读 WebSocket 链路：

1. [`backend/app/main.py`](backend/app/main.py) 的 `/ws/{canvas_id}`
2. [`backend/app/services/connection_manager.py`](backend/app/services/connection_manager.py)
3. [`backend/app/routers/chat.py`](backend/app/routers/chat.py) 中的广播逻辑
4. [`frontend/src/components/ChatInterface.tsx`](frontend/src/components/ChatInterface.tsx) 中的 WebSocket `useEffect`
5. [`polystudio-client/SKILL.md`](polystudio-client/SKILL.md)

理解两种流的区别：

| 通道 | 主要场景 | 数据方向 |
| --- | --- | --- |
| SSE | 当前页面主动发起聊天 | 当前 HTTP 请求返回事件 |
| WebSocket | 外部客户端驱动画布 | 后端向订阅画布实时广播 |

有余力再浏览：

- [`frontend/src/components/SettingsPage.tsx`](frontend/src/components/SettingsPage.tsx)
- [`backend/app/routers/settings.py`](backend/app/routers/settings.py)
- [`frontend/src/components/Model3DViewer.tsx`](frontend/src/components/Model3DViewer.tsx)

总结实践三选一：

- [ ] 为 `HistoryService` 添加保存、更新、删除测试
- [ ] 添加一个不依赖外部 API 的简单 Agent Tool
- [ ] 添加一种 SSE 事件，并完成前后端处理

## 五、大文件阅读策略

以下文件职责较多，不建议从第一行通读到最后一行：

- `frontend/src/components/ChatInterface.tsx`：约 2000 行
- `frontend/src/components/ExcalidrawCanvas.tsx`：约 1400 行
- `frontend/src/components/SettingsPage.tsx`：约 1000 行

推荐做法：

1. 先看组件 Props、类型和顶层状态。
2. 根据具体功能搜索函数名或 API 路径。
3. 沿一次用户操作追踪相关函数。
4. 最后再看渲染 JSX 和样式。

例如学习聊天时，只搜索：

```text
/api/chat
getReader
event.type
tool_result
```

学习画布时，只搜索：

```text
addImage
addVideo
add3DModelPreview
onDataChange
```

## 六、学习笔记模板

每学习一个模块，可以复制下面的模板：

```markdown
### 模块名称

- 文件：
- 职责：
- 输入：
- 输出：
- 上游调用者：
- 下游依赖：
- 核心数据结构：
- 异常处理：
- 我还不理解的问题：
```

## 七、最终验收清单

- [ ] 不看代码画出完整聊天调用链
- [ ] 说清楚 `delta`、`tool_call`、`tool_result` 的产生和消费位置
- [ ] 说清楚媒体 URL 如何从工具结果进入 Excalidraw
- [ ] 说清楚画布和消息如何保存、恢复
- [ ] 说清楚 SSE 与 WebSocket 的职责差异
- [ ] 说清楚 Agent Prompt、LLM 和 Tools 的关系
- [ ] 说清楚 Skill 为什么采用渐进加载
- [ ] 能给一个新功能判断应该放在 router、service、tool 还是 frontend
- [ ] 完成至少一个小型代码练习

## 八、注意事项

- [`backend/FRAMEWORK.md`](backend/FRAMEWORK.md) 可以帮助理解早期架构，但部分内容落后于当前实现，应以源码为准。
- 项目目前没有成体系的测试目录。学习过程中补充 `HistoryService`、事件转换等纯逻辑测试，是很合适的入门实践。
- 图片、视频、3D、TTS 等工具大多依赖外部服务。学习架构时优先关注工具接口、输入输出和事件流，不必一开始研究每家 API 的全部细节。
- 调试时不要在笔记、截图或提交记录中暴露 `.env` 内的 API Key。

