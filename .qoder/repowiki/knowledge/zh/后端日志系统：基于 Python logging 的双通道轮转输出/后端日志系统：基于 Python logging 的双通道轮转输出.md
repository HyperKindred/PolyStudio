---
kind: logging_system
name: 后端日志系统：基于 Python logging 的双通道轮转输出
category: logging_system
scope:
    - '**'
source_files:
    - backend/app/utils/logger.py
    - backend/app/main.py
    - backend/app/llm/factory.py
    - backend/app/llm/siliconflow.py
    - backend/app/llm/volcano.py
    - backend/app/routers/chat.py
    - backend/app/routers/settings.py
    - backend/app/services/agent_service.py
---

## 1. 使用的系统与框架
- 基于 Python 标准库 `logging` + `logging.handlers.RotatingFileHandler`，未引入第三方日志框架（如 loguru、structlog）。
- 应用启动时在 FastAPI 主入口统一初始化，所有模块通过 `logging.getLogger(__name__)` 获取子 logger。

## 2. 核心文件与位置
- 配置入口：`backend/app/utils/logger.py` — 提供 `setup_logging()` 和 `get_logger()`。
- 应用启动注入：`backend/app/main.py` 中读取环境变量 `LOG_LEVEL` 并调用 `setup_logging(log_level=...)`。
- 使用方示例：`app/llm/factory.py`、`app/llm/siliconflow.py`、`app/llm/volcano.py`、`app/routers/chat.py`、`app/routers/settings.py`、`app/services/agent_service.py` 等均采用 `logger = logging.getLogger(__name__)` 模式。
- 运行时日志目录：`backend/logs/`，按天生成 `polystudio_YYYYMMDD.log` 与 `polystudio_error_YYYYMMDD.log`。

## 3. 架构与约定
- 双通道输出：控制台使用 `StreamHandler`，简单格式，级别受 `LOG_LEVEL` 控制；文件使用 `RotatingFileHandler`，详细格式包含文件名与行号，固定 `DEBUG` 级别，单文件最大 10MB，保留 5 个备份；错误独立写入 `polystudio_error_YYYYMMDD.log`，仅记录 `ERROR` 及以上。
- 第三方库降噪：`httpx`、`httpcore`、`urllib3` 默认降级到 `WARNING`，避免 HTTP 客户端噪音淹没业务日志。
- 命名空间：各模块以 `__name__` 作为 logger 名称，形成 `app.llm.factory`、`app.routers.chat` 等层级结构，便于按模块过滤。

## 4. 开发者应遵循的规则
- 统一初始化：不要在模块内重复 `basicConfig`，一律在应用启动时通过 `setup_logging` 配置一次。
- 获取 logger：使用 `import logging; logger = logging.getLogger(__name__)`，不要直接调用全局 `logging.info(...)`。
- 日志级别选择：`DEBUG` 用于调试细节，`INFO` 用于关键流程节点，`WARNING` 用于可恢复异常，`ERROR` 用于不可恢复错误且必须记录到错误日志文件。
- 敏感信息：避免在日志中打印密钥、token、用户隐私数据。
- 性能注意：文件 handler 固定 `DEBUG`，生产环境可通过容器编排或进程管理限制磁盘写入量。