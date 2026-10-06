"""SubAgent 接口"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

class ISubAgent(ABC):
    """子代理接口 - 垂直任务拆分"""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """子代理能力描述（LLM可见）"""
        pass

    @abstractmethod
    async def run(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        parent_ctx: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        执行子代理任务

        Returns:
            {"success": bool, "final_answer": str, "metadata": dict}
        """
        pass

    @property
    def max_steps(self) -> int:
        """子代理内部最大步数"""
        return 10
