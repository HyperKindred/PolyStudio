---
kind: error_handling
name: FastAPI 路由层异常与 SSE 流式错误处理
category: error_handling
scope:
    - '**'
source_files:
    - backend/app/main.py
    - backend/app/routers/chat.py
    - backend/app/services/stream_processor.py
    - backend/app/utils/logger.py
    - backend/app/llm/factory.py
---

## 1. 采用的体系与模式
- HTTP 层：基于 FastAPI，统一通过 fastapi.HTTPException 抛出业务/参数错误（400、500），由框架自动转为 JSON 响应。
- WebSocket/SSE 层：在 /ws/{canvas_id} 中捕获 WebSocketDisconnect；SSE 流式响应在 StreamProcessor 内部将异常包装为 {type: "error", error, traceback} 事件继续下发，而非直接中断连接。
- 日志系统：通过 app.utils.logger.setup_logging 初始化，同时输出到控制台和按大小轮转的文件，并单独维护 polystudio_error_*.log 仅记录 ERROR 及以上级别。
- 无全局异常中间件或自定义异常类，错误处理分散在各路由与服务函数内。

## 2. 关键文件与位置
- backend/app/main.py：应用入口，注册 CORS、静态文件、路由；WebSocket 端点捕获 WebSocketDisconnect。
- backend/app/routers/chat.py：所有上传与聊天接口，使用 try/except Exception + HTTPException 做边界错误处理。
- backend/app/services/stream_processor.py：LangGraph 流式处理器，负责把底层异常转换为 SSE data:{"type":"error"} 事件。
- backend/app/utils/logger.py：统一的日志配置（控制台 + RotatingFileHandler + 独立 error 文件）。
- backend/app/llm/factory.py：LLM 工厂，对非法 provider 抛 ValueError，具体实现抛 RuntimeError，由上层捕获后转为 HTTP 500。

## 3. 架构与约定
- 路由层约定
  - 参数校验失败 -> 主动 raise HTTPException(status_code=400, detail=...)。
  - 其他未预期异常 -> except Exception as e: 捕获后 raise HTTPException(status_code=500, detail=f"...: {str(e)}")。
  - 上传接口中先 except HTTPException: raise 再 except Exception，避免重复包装。
- SSE 流式约定
  - 正常完成发送 data: [DONE]。
  - 任何阶段异常都 yield 一个 data:{"type":"error","error":...,"traceback":...} 事件，保证前端能收到结构化错误。
  - 客户端断开时捕获 GeneratorExit / StopAsyncIteration / ConnectionError / BrokenPipeError / OSError，记录日志并停止上游 agent 处理。
- WebSocket 约定
  - 连接建立后 while True: await receive_text()，断开时调用 manager.disconnect 清理资源。
- 日志约定
  - 模块级 logger = logging.getLogger(__name__)，关键路径用 info/warning/error 分级记录。
  - 错误堆栈通过 traceback.format_exc() 一并写入 error 日志文件。

## 4. 开发者应遵循的规则
- 对外暴露的错误一律通过 HTTPException 返回，不要直接返回裸异常对象或字符串。
- 需要区分用户输入/参数错误和服务器内部错误，分别使用 4xx 与 5xx 状态码。
- 在流式接口中，不要吞掉异常；应将异常封装为 {type:"error", ...} 事件继续下发，确保前端可感知。
- 涉及 I/O 的长流程（上传、写历史、调用 LLM）必须包裹 try/except Exception，并在 except 中记录日志后再抛出 HTTP 错误或 SSE error 事件。
- 新增路由或服务方法时，复用 app.utils.logger.get_logger(__name__) 获取 logger，避免重复配置根 logger。
- 不要在路由层使用 logging.error 代替 HTTPException 作为错误传播手段；日志用于可观测性，HTTP 响应才是错误契约。
- 如需新增业务异常类型，建议定义统一基类并在路由层集中捕获转换，当前仓库尚未引入此类抽象，后续演进可考虑。