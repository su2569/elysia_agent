"""SubAgent Registry - 子代理注册中心

支持全局默认注册表 + 隔离实例模式。
"""
from typing import Dict, Any, Optional
from ..interfaces.subagent import ISubAgent


class SubAgentRegistry:
    """子代理注册中心"""

    _default_instance: Optional["SubAgentRegistry"] = None

    def __init__(self):
        self._agents: Dict[str, ISubAgent] = {}

    # ---------- 实例方法 ----------

    def register_instance(self, agent: ISubAgent) -> None:
        self._agents[agent.name] = agent

    def get_instance(self, name: str) -> Optional[ISubAgent]:
        return self._agents.get(name)

    def unregister_instance(self, name: str) -> bool:
        if name in self._agents:
            del self._agents[name]
            return True
        return False

    def list_instance(self) -> Dict[str, ISubAgent]:
        return dict(self._agents)

    def clear_instance(self) -> None:
        self._agents.clear()

    # ---------- 类方法（操作全局默认实例） ----------

    @classmethod
    def _get_default(cls) -> "SubAgentRegistry":
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    @classmethod
    def register(cls, agent: ISubAgent) -> None:
        cls._get_default().register_instance(agent)

    @classmethod
    def get(cls, name: str) -> Optional[ISubAgent]:
        return cls._get_default().get_instance(name)

    @classmethod
    def unregister(cls, name: str) -> bool:
        return cls._get_default().unregister_instance(name)

    @classmethod
    def list(cls) -> Dict[str, ISubAgent]:
        return cls._get_default().list_instance()

    @classmethod
    def clear(cls) -> None:
        cls._get_default().clear_instance()

    @classmethod
    def create_isolated(cls) -> "SubAgentRegistry":
        """创建隔离实例（用于测试）"""
        return cls()
