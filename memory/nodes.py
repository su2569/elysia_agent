"""Memory Node System - 参考 APLR 的记忆节点设计

记忆节点：实体（人物、地点、概念）的结构化存储
支持：节点创建、更新、检索、关联

使用 aiosqlite 实现真正的异步非阻塞数据库操作。
"""
import asyncio
import json
import aiosqlite
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path

from ..utils.logger import get_logger

logger = get_logger("MemoryNodes")


@dataclass
class MemoryNode:
    """记忆节点 - 代表一个实体或概念"""
    id: str
    name: str                    # 节点名称
    node_type: str               # person | place | concept | event | theme
    description: str             # 节点描述
    related_nodes: List[str] = None  # 关联节点ID列表
    importance: float = 0.5      # 重要性 0.0~1.0
    emotion_tags: List[str] = None   # 情绪标签
    first_seen: str = ""         # 首次出现日期
    last_updated: str = ""       # 最后更新日期
    occurrence_count: int = 1    # 出现次数
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.related_nodes is None:
            self.related_nodes = []
        if self.emotion_tags is None:
            self.emotion_tags = []
        if self.metadata is None:
            self.metadata = {}
        if not self.first_seen:
            self.first_seen = datetime.now().isoformat()
        if not self.last_updated:
            self.last_updated = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryNode":
        return cls(**data)


@dataclass
class MemoryEvent:
    """记忆事件 - 一次对话或经历"""
    id: str
    date: str                    # 事件日期 YYYY-MM-DD
    narrative: str             # 事件叙述
    reflection: str              # 深度感想/反思
    participants: List[str] = None  # 参与者
    related_nodes: List[str] = None  # 关联节点
    importance: float = 0.5
    emotion_intensity: float = 0.0   # 情感强度
    keywords: List[str] = None
    vector: Optional[List[float]] = None  # 嵌入向量
    source_session: str = ""     # 来源会话

    def __post_init__(self):
        if self.participants is None:
            self.participants = []
        if self.related_nodes is None:
            self.related_nodes = []
        if self.keywords is None:
            self.keywords = []


@dataclass
class DailySummary:
    """每日总结"""
    date: str
    events: List[MemoryEvent] = None
    insights: str = ""           # 每日感悟
    mood_trend: str = ""         # 情绪趋势
    key_nodes_updated: List[str] = None  # 更新的关键节点

    def __post_init__(self):
        if self.events is None:
            self.events = []
        if self.key_nodes_updated is None:
            self.key_nodes_updated = []


@dataclass
class MemoryTheme:
    """记忆主题 - 主题聚类结果"""
    id: str
    name: str                    # 主题名称
    description: str             # 主题描述
    event_ids: List[str] = None  # 包含的事件ID
    node_ids: List[str] = None   # 关联节点ID
    created_at: str = ""

    def __post_init__(self):
        if self.event_ids is None:
            self.event_ids = []
        if self.node_ids is None:
            self.node_ids = []
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class MemoryNodeManager:
    """
    记忆节点管理器（异步版）

    参考 APLR 设计：
    - 节点提取与存储
    - 节点关联
    - 节点检索
    - 主题聚类
    """

    def __init__(self, db_path: str = "./data/memory_nodes.sqlite"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_init(self):
        """延迟初始化数据库（协程安全）"""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("PRAGMA journal_mode=WAL")

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_nodes (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        node_type TEXT NOT NULL,
                        description TEXT,
                        related_nodes TEXT,       -- JSON array
                        importance REAL DEFAULT 0.5,
                        emotion_tags TEXT,        -- JSON array
                        first_seen TEXT,
                        last_updated TEXT,
                        occurrence_count INTEGER DEFAULT 1,
                        metadata TEXT             -- JSON
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_events (
                        id TEXT PRIMARY KEY,
                        date TEXT NOT NULL,
                        narrative TEXT,
                        reflection TEXT,
                        participants TEXT,       -- JSON array
                        related_nodes TEXT,        -- JSON array
                        importance REAL DEFAULT 0.5,
                        emotion_intensity REAL DEFAULT 0.0,
                        keywords TEXT,             -- JSON array
                        vector TEXT,               -- JSON array (embedding)
                        source_session TEXT
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_summaries (
                        date TEXT PRIMARY KEY,
                        events TEXT,                 -- JSON array of event IDs
                        insights TEXT,
                        mood_trend TEXT,
                        key_nodes_updated TEXT       -- JSON array
                    )
                """)

                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_themes (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        description TEXT,
                        event_ids TEXT,              -- JSON array
                        node_ids TEXT,               -- JSON array
                        created_at TEXT
                    )
                """)

                await conn.execute("CREATE INDEX IF NOT EXISTS idx_node_name ON memory_nodes(name)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_node_type ON memory_nodes(node_type)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_event_date ON memory_events(date)")

                await conn.commit()
            self._initialized = True

    # ===== 节点操作 =====

    async def create_or_update_node(self, node: MemoryNode) -> MemoryNode:
        """创建或更新节点"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM memory_nodes WHERE name = ? AND node_type = ?",
                (node.name, node.node_type)
            ) as cursor:
                existing = await cursor.fetchone()

            if existing:
                old_count = existing[9]  # occurrence_count
                await conn.execute("""
                    UPDATE memory_nodes SET
                        description = ?,
                        related_nodes = ?,
                        importance = ?,
                        emotion_tags = ?,
                        last_updated = ?,
                        occurrence_count = ?,
                        metadata = ?
                    WHERE id = ?
                """, (
                    node.description,
                    json.dumps(node.related_nodes),
                    max(node.importance, existing[5]),
                    json.dumps(node.emotion_tags),
                    datetime.now().isoformat(),
                    old_count + 1,
                    json.dumps(node.metadata),
                    existing[0]
                ))
                node.id = existing[0]
                node.occurrence_count = old_count + 1
            else:
                if not node.id:
                    import uuid
                    node.id = f"node_{uuid.uuid4().hex[:8]}"
                await conn.execute("""
                    INSERT INTO memory_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    node.id, node.name, node.node_type, node.description,
                    json.dumps(node.related_nodes), node.importance,
                    json.dumps(node.emotion_tags), node.first_seen,
                    node.last_updated, node.occurrence_count,
                    json.dumps(node.metadata)
                ))
            await conn.commit()
        return node

    async def get_node(self, name: str, node_type: Optional[str] = None) -> Optional[MemoryNode]:
        """获取节点"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            if node_type:
                async with conn.execute(
                    "SELECT * FROM memory_nodes WHERE name = ? AND node_type = ?",
                    (name, node_type)
                ) as cursor:
                    row = await cursor.fetchone()
            else:
                async with conn.execute(
                    "SELECT * FROM memory_nodes WHERE name = ?",
                    (name,)
                ) as cursor:
                    row = await cursor.fetchone()

            if row:
                return MemoryNode(
                    id=row[0], name=row[1], node_type=row[2], description=row[3],
                    related_nodes=json.loads(row[4]) if row[4] else [],
                    importance=row[5],
                    emotion_tags=json.loads(row[6]) if row[6] else [],
                    first_seen=row[7], last_updated=row[8],
                    occurrence_count=row[9],
                    metadata=json.loads(row[10]) if row[10] else {}
                )
            return None

    async def search_nodes(self, query: str, limit: int = 10) -> List[MemoryNode]:
        """搜索节点"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM memory_nodes WHERE name LIKE ? OR description LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit)
            ) as cursor:
                rows = await cursor.fetchall()

                return [MemoryNode(
                    id=r[0], name=r[1], node_type=r[2], description=r[3],
                    related_nodes=json.loads(r[4]) if r[4] else [],
                    importance=r[5],
                    emotion_tags=json.loads(r[6]) if r[6] else [],
                    first_seen=r[7], last_updated=r[8],
                    occurrence_count=r[9],
                    metadata=json.loads(r[10]) if r[10] else {}
                ) for r in rows]

    async def get_all_nodes(self, node_type: Optional[str] = None) -> List[MemoryNode]:
        """获取所有节点"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            if node_type:
                async with conn.execute(
                    "SELECT * FROM memory_nodes WHERE node_type = ?",
                    (node_type,)
                ) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with conn.execute("SELECT * FROM memory_nodes") as cursor:
                    rows = await cursor.fetchall()

            return [MemoryNode(
                id=r[0], name=r[1], node_type=r[2], description=r[3],
                related_nodes=json.loads(r[4]) if r[4] else [],
                importance=r[5],
                emotion_tags=json.loads(r[6]) if r[6] else [],
                first_seen=r[7], last_updated=r[8],
                occurrence_count=r[9],
                metadata=json.loads(r[10]) if r[10] else {}
            ) for r in rows]

    # ===== 事件操作 =====

    async def store_event(self, event: MemoryEvent) -> MemoryEvent:
        """存储事件"""
        await self._ensure_init()
        if not event.id:
            import uuid
            event.id = f"evt_{uuid.uuid4().hex[:8]}"

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO memory_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.id, event.date, event.narrative, event.reflection,
                json.dumps(event.participants), json.dumps(event.related_nodes),
                event.importance, event.emotion_intensity,
                json.dumps(event.keywords),
                json.dumps(event.vector) if event.vector else None,
                event.source_session
            ))
            await conn.commit()
        return event

    async def get_events_by_date(self, date: str) -> List[MemoryEvent]:
        """获取指定日期的事件"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM memory_events WHERE date = ?",
                (date,)
            ) as cursor:
                rows = await cursor.fetchall()

                return [MemoryEvent(
                    id=r[0], date=r[1], narrative=r[2], reflection=r[3],
                    participants=json.loads(r[4]) if r[4] else [],
                    related_nodes=json.loads(r[5]) if r[5] else [],
                    importance=r[6], emotion_intensity=r[7],
                    keywords=json.loads(r[8]) if r[8] else [],
                    vector=json.loads(r[9]) if r[9] else None,
                    source_session=r[10]
                ) for r in rows]

    async def get_recent_events(self, days: int = 7, min_importance: float = 0.0) -> List[MemoryEvent]:
        """获取近期事件"""
        await self._ensure_init()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM memory_events WHERE date >= ? AND importance >= ? ORDER BY date DESC",
                (cutoff, min_importance)
            ) as cursor:
                rows = await cursor.fetchall()

                return [MemoryEvent(
                    id=r[0], date=r[1], narrative=r[2], reflection=r[3],
                    participants=json.loads(r[4]) if r[4] else [],
                    related_nodes=json.loads(r[5]) if r[5] else [],
                    importance=r[6], emotion_intensity=r[7],
                    keywords=json.loads(r[8]) if r[8] else [],
                    vector=json.loads(r[9]) if r[9] else None,
                    source_session=r[10]
                ) for r in rows]

    # ===== 每日总结 =====

    async def store_daily_summary(self, summary: DailySummary) -> None:
        """存储每日总结"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO daily_summaries VALUES (?, ?, ?, ?, ?)
            """, (
                summary.date,
                json.dumps([e.id for e in summary.events]),
                summary.insights,
                summary.mood_trend,
                json.dumps(summary.key_nodes_updated)
            ))
            await conn.commit()

    async def get_daily_summary(self, date: str) -> Optional[DailySummary]:
        """获取每日总结"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(
                "SELECT * FROM daily_summaries WHERE date = ?",
                (date,)
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                event_ids = json.loads(row[1]) if row[1] else []
                events = []
                for eid in event_ids:
                    async with conn.execute("SELECT * FROM memory_events WHERE id = ?", (eid,)) as cur:
                        er = await cur.fetchone()
                        if er:
                            events.append(MemoryEvent(
                                id=er[0], date=er[1], narrative=er[2], reflection=er[3]
                            ))

                return DailySummary(
                    date=row[0], events=events, insights=row[2],
                    mood_trend=row[3],
                    key_nodes_updated=json.loads(row[4]) if row[4] else []
                )
            return None

    # ===== 主题聚类 =====

    async def store_theme(self, theme: MemoryTheme) -> None:
        """存储主题"""
        await self._ensure_init()
        if not theme.id:
            import uuid
            theme.id = f"theme_{uuid.uuid4().hex[:8]}"

        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO memory_themes VALUES (?, ?, ?, ?, ?, ?)
            """, (
                theme.id, theme.name, theme.description,
                json.dumps(theme.event_ids), json.dumps(theme.node_ids),
                theme.created_at
            ))
            await conn.commit()

    async def get_themes(self) -> List[MemoryTheme]:
        """获取所有主题"""
        await self._ensure_init()
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute("SELECT * FROM memory_themes") as cursor:
                rows = await cursor.fetchall()
                return [MemoryTheme(
                    id=r[0], name=r[1], description=r[2],
                    event_ids=json.loads(r[3]) if r[3] else [],
                    node_ids=json.loads(r[4]) if r[4] else [],
                    created_at=r[5]
                ) for r in rows]

    # ===== 多维权重回忆排序 (参考APLR) =====

    async def recall_with_ranking(
        self,
        query: str,
        query_vector: Optional[List[float]] = None,
        top_k: int = 10,
        weights: Optional[Dict[str, float]] = None
    ) -> List[Tuple[MemoryEvent, float]]:
        """
        多维权重回忆排序（异步版）

        参考 APLR 设计：
        - 语义相关度 (semantic): 向量相似度
        - 重要性 (importance): 事件重要性
        - 情感强度 (emotion): 情感强度
        - 时间衰减 (time_decay): 随时间衰减
        - 稀有词汇 (rarity): 关键词稀有度
        """
        await self._ensure_init()
        weights = weights or {"semantic": 1.0, "importance": 1.0, "emotion": 1.0, "time_decay": 1.0, "rarity": 1.0}

        events = await self.get_recent_events(days=365)

        # 计算关键词频率（用于稀有度）
        keyword_freq = {}
        for e in events:
            for kw in e.keywords:
                keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

        scored_events = []
        for event in events:
            score = 0.0

            # 1. 语义相关度 (简化版：关键词匹配)
            semantic_score = 0.0
            query_words = set(query.lower().split())
            event_words = set(" ".join(event.keywords).lower().split())
            if query_words & event_words:
                semantic_score = len(query_words & event_words) / len(query_words | event_words)
            score += semantic_score * weights.get("semantic", 1.0)

            # 2. 重要性
            score += event.importance * weights.get("importance", 1.0)

            # 3. 情感强度
            score += event.emotion_intensity * weights.get("emotion", 1.0)

            # 4. 时间衰减 (越新越高)
            try:
                event_date = datetime.strptime(event.date, "%Y-%m-%d")
                days_ago = (datetime.now() - event_date).days
                time_decay = max(0, 1 - days_ago / 365)
            except Exception:
                time_decay = 0.5
            score += time_decay * weights.get("time_decay", 1.0)

            # 5. 稀有词汇
            rarity_score = 0.0
            for kw in event.keywords:
                freq = keyword_freq.get(kw, 1)
                rarity_score += 1.0 / freq
            if event.keywords:
                rarity_score /= len(event.keywords)
            score += rarity_score * weights.get("rarity", 1.0)

            scored_events.append((event, score))

        scored_events.sort(key=lambda x: x[1], reverse=True)
        return scored_events[:top_k]
