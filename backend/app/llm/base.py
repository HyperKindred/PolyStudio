"""LLM Provider 的统一抽象接口。

这个模块不创建任何具体模型，只规定每个模型供应商实现必须提供哪些能力。
上层 ``llm/factory.py`` 和 ``agent_service.py`` 因此可以面向统一接口编程，而不用
分别依赖火山引擎、SiliconFlow 或未来新增供应商的具体类。

对应关系：

    BaseLLMProvider（抽象规范）
        ├── VolcanoLLMProvider（火山引擎实现）
        └── SiliconFlowLLMProvider（SiliconFlow 实现）
"""
from abc import ABC, abstractmethod
from typing import Any, Optional
from langchain_core.language_models import BaseChatModel


class BaseLLMProvider(ABC):
    """所有 LLM 供应商适配器必须继承的抽象基类。

    ``ABC`` 是 Abstract Base Class（抽象基类）。该类用于定义接口和约束，
    不能直接承担具体模型配置；子类必须实现所有标记为 ``@abstractmethod`` 的方法。
    """
    
    # @abstractmethod 表示这是接口要求，而不是已经完成的实现。
    # 如果子类没有实现 create_model()，Python 会阻止其实例化。
    @abstractmethod
    def create_model(self) -> BaseChatModel:
        """创建一个 LangChain 兼容的聊天模型实例。

        每个供应商可以在自己的实现中读取不同的 API Key、Base URL、模型名称和
        特殊参数，但最终都要返回 ``BaseChatModel``。LangGraph 只依赖这套统一的
        invoke、stream、bind_tools 等模型接口。

        Returns:
            配置完成、可以交给 ``create_react_agent`` 使用的聊天模型。
        """
        # 抽象方法没有通用实现；真正逻辑位于 VolcanoLLMProvider 等子类中。
        pass
    
    # Provider 名称用于日志、配置选择和排查当前实际使用的模型供应商。
    @abstractmethod
    def get_provider_name(self) -> str:
        """返回供应商的稳定标识名称。

        Returns:
            例如 ``"volcano"`` 或 ``"siliconflow"``。
        """
        pass
