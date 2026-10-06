"""Memory Service 接口 - 适配 ChatDB"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

class IMemoryService(ABC):
    """
    记忆服务接口

    双注入记忆引擎：
    - retrieve: 检索记忆（注入点A/B）
    - store: 存储记忆（被动/主动）
    """

    @abstractmethod
    async def retrieve(
        self,
        user_id: str,
        query: str,
        group_id: Optional[str] = None,
        include_recent: int = 3,
        include_semantic: int = 3,
        include_profile: bool = True,
        include_active: bool = False,
        include_emotion: bool = False,
        include_tool_history: bool = False,
        **filters
    ) -> Dict[str, Any]:
        """
        检索记忆

        Returns:
            {
                "profile": str,          # 用户画像
                "recent": List[dict],    # 近期对话
                "semantic": List[dict],  # 语义相似记忆
                "active": List[dict],    # 主动规则/经验
                "emotion_history": str,  # 情绪历史
                "tool_history": List[dict] # 工具使用历史
            }
        """
        pass

    @abstractmethod
    async def store(
        self,
        user_id: str,
        memory_type: str,  # "passive" | "active" | "emotion" | "summary"
        data: Dict[str, Any],
        group_id: Optional[str] = None,
        importance: float = 0.5,
        tags: Optional[List[str]] = None
    ) -> None:
        """
        存储记忆

        Args:
            memory_type: 记忆类型
                - passive: 被动记忆（自动捕获的完整轨迹）
                - active: 主动记忆（提炼的规则、偏好）
                - emotion: 情绪记录
                - summary: 总结记忆
            importance: 重要性评分 0.0~1.0
            tags: 标签列表
        """
        pass

    @abstractmethod
    async def delete(self, memory_id: str) -> bool:
        """删除记忆"""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        search_type: str = "hybrid",  # fulltext | semantic | hybrid | time
        group_id: Optional[str] = None,
        limit: int = 10,
        **filters
    ) -> List[Dict[str, Any]]:
        """搜索记忆"""
        pass

    @abstractmethod
    async def summarize(self, user_id: str, level: str = "24h") -> Dict[str, Any]:
        """
        触发记忆总结

        Args:
            level: 总结层级 3h | 12h | 24h | month | year
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass
