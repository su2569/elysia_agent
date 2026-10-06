"""Enhanced Memory Adapter - 集成 APLR 记忆节点 + 双注入记忆

结合：
- 原 ChatDB/SQLite 基础记忆存储
- APLR 记忆节点系统（事件、节点、主题、每日总结）
- 多维权重回忆排序
- 自动历史注入

注意：所有 MemoryNodeManager 方法已改为异步，调用需加 await。
"""
import json
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from .sqlite_adapter import SQLiteMemory
from .nodes import MemoryNodeManager, MemoryNode, MemoryEvent, DailySummary, MemoryTheme
from ..interfaces.memory import IMemoryService
from ..utils.logger import get_logger

logger = get_logger("EnhancedMemory")


class EnhancedMemory(IMemoryService):
    """
    增强型记忆适配器

    融合原 SQLiteMemory + APLR 记忆节点系统
    """

    def __init__(self, db_path: str = "./data/elysia_memory.sqlite", node_db_path: str = "./data/memory_nodes.sqlite"):
        self.base_memory = SQLiteMemory(db_path=db_path)
        self.node_manager = MemoryNodeManager(db_path=node_db_path)
        logger.info("EnhancedMemory initialized with node system")

    async def retrieve(
        self, user_id: str, query: str, group_id: Optional[str] = None,
        include_recent: int = 3, include_semantic: int = 3,
        include_profile: bool = True, include_active: bool = False,
        include_emotion: bool = False, include_tool_history: bool = False,
        **filters
    ) -> Dict[str, Any]:
        """检索记忆（增强版 - 全异步）"""
        # 基础记忆检索
        base_result = await self.base_memory.retrieve(
            user_id=user_id, query=query, group_id=group_id,
            include_recent=include_recent, include_semantic=include_semantic,
            include_profile=include_profile, include_active=include_active,
            include_emotion=include_emotion, include_tool_history=include_tool_history,
            **filters
        )

        # 记忆节点检索（APLR风格）- 全部加 await
        nodes = await self.node_manager.search_nodes(query, limit=5)
        related_events = await self.node_manager.recall_with_ranking(query, top_k=5)

        # 近期重要事件
        recent_events = await self.node_manager.get_recent_events(days=3, min_importance=0.6)

        # 每日总结
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_summary = await self.node_manager.get_daily_summary(yesterday)

        # 主题
        themes = await self.node_manager.get_themes()

        return {
            **base_result,
            "nodes": [n.to_dict() for n in nodes],
            "related_events": [{"id": e.id, "narrative": e.narrative, "score": s} for e, s in related_events],
            "recent_important_events": [e.narrative for e in recent_events[:3]],
            "daily_summary": daily_summary.insights if daily_summary else "",
            "themes": [t.name for t in themes[:3]],
        }

    async def store(
        self, user_id: str, memory_type: str, data: Dict[str, Any],
        group_id: Optional[str] = None, importance: float = 0.5,
        tags: Optional[List[str]] = None
    ) -> None:
        """存储记忆"""
        # 基础存储
        await self.base_memory.store(user_id, memory_type, data, group_id, importance, tags)

        # 如果是被动记忆，同时提取事件和节点
        if memory_type == "passive" and isinstance(data, dict):
            # 提取事件
            if "final_answer" in data and "query" in data:
                event = MemoryEvent(
                    id="",
                    date=datetime.now().strftime("%Y-%m-%d"),
                    narrative=f"用户: {data['query']}\nAI: {data['final_answer']}",
                    reflection="",
                    participants=[user_id],
                    importance=importance,
                    source_session=data.get("session_id", "")
                )
                await self.node_manager.store_event(event)

    async def delete(self, memory_id: str) -> bool:
        return await self.base_memory.delete(memory_id)

    async def search(
        self, query: str, search_type: str = "hybrid",
        group_id: Optional[str] = None, limit: int = 10, **filters
    ) -> List[Dict[str, Any]]:
        """搜索记忆（增强版，支持节点搜索）"""
        base_results = await self.base_memory.search(query, search_type, group_id, limit, **filters)

        # 同时搜索节点
        nodes = await self.node_manager.search_nodes(query, limit=limit)
        node_results = [{"type": "node", **n.to_dict()} for n in nodes]

        return base_results + node_results

    async def summarize(self, user_id: str, level: str = "24h") -> Dict[str, Any]:
        return await self.base_memory.summarize(user_id, level)

    async def health_check(self) -> bool:
        return await self.base_memory.health_check()

    # ===== APLR 扩展方法（全部改为 async） =====

    async def create_node(self, name: str, node_type: str, description: str, **kwargs) -> MemoryNode:
        """创建记忆节点"""
        node = MemoryNode(id="", name=name, node_type=node_type, description=description, **kwargs)
        return await self.node_manager.create_or_update_node(node)

    async def get_node(self, name: str, node_type: Optional[str] = None) -> Optional[MemoryNode]:
        """获取记忆节点"""
        return await self.node_manager.get_node(name, node_type)

    async def recall_nodes(self, query: str, limit: int = 5) -> List[MemoryNode]:
        """回忆节点"""
        return await self.node_manager.search_nodes(query, limit)

    async def store_daily_summary(self, date: str, events: List[MemoryEvent], insights: str, **kwargs) -> None:
        """存储每日总结"""
        summary = DailySummary(date=date, events=events, insights=insights, **kwargs)
        await self.node_manager.store_daily_summary(summary)

    async def get_daily_summary(self, date: str) -> Optional[DailySummary]:
        """获取每日总结"""
        return await self.node_manager.get_daily_summary(date)

    async def get_new_session_context(self, days_important: int = 3, days_summary: int = 7) -> str:
        """
        获取新会话上下文注入内容（APLR风格）

        新对话时自动注入：
        - 近N天重要事件
        - 近N天每日总结
        """
        parts = []

        # 近期重要事件
        recent = await self.node_manager.get_recent_events(days=days_important, min_importance=0.7)
        if recent:
            parts.append("【近期重要事件】")
            for e in recent[:5]:
                parts.append(f"- {e.date}: {e.narrative[:80]}")

        # 近期总结
        summaries = []
        for i in range(days_summary):
            d = (datetime.now() - timedelta(days=i+1)).strftime("%Y-%m-%d")
            s = await self.node_manager.get_daily_summary(d)
            if s and s.insights:
                summaries.append(f"- {d}: {s.insights[:100]}")

        if summaries:
            parts.append("\n【近期感悟】")
            parts.extend(summaries[:3])

        return "\n".join(parts) if parts else ""

    async def auto_recall(self, query: str, top_k: int = 5, weights: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
        """
        自动回忆（APLR风格）

        聊天时自动检索相关记忆并注入
        """
        results = await self.node_manager.recall_with_ranking(query, top_k=top_k, weights=weights)
        return [
            {
                "event_id": e.id,
                "narrative": e.narrative,
                "reflection": e.reflection,
                "score": score,
                "date": e.date,
            }
            for e, score in results
        ]
