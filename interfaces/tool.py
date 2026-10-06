"""Tool 接口"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class ITool(ABC):
    """工具接口 - 所有工具必须实现"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具唯一标识名"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述（LLM可见）"""
        pass

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        """JSON Schema格式的参数定义"""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """
        执行工具

        特殊参数：
            _ctx: AgentContext（由Runner自动注入）
        """
        pass

    @property
    def category(self) -> str:
        """工具分类"""
        return "general"

    @property
    def is_async(self) -> bool:
        return True
