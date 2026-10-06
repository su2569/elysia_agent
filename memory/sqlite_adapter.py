"""SQLite Memory Adapter - 纯Python本地记忆（无ChatDB时回退）

使用 aiosqlite 实现真正的异步非阻塞数据库操作。

v2.2.2 起采用「单持久连接」架构：
- 每个 SQLiteMemory 实例维护一条持久 aiosqlite 连接，整个生命周期复用；
- 避免每次操作 open/close 连接（每个连接=一个后台线程）在 Windows 上引发的
  线程风暴与 WAL 锁竞争（此前会在高频写时表现为"测试挂起"）；
- 连接级 `PRAGMA synchronous=NORMAL`（WAL 模式官方推荐组合），commit 提速约 100 倍
  （Windows 上 FULL 模式每次 commit 触发完整 fsync，约 100ms/次）。
"""
import asyncio
import json
import os
import aiosqlite
from typing import Dict, Any, Optional, List
from pathlib import Path

from ..interfaces.memory import IMemoryService
from ..utils.logger import get_logger

logger = get_logger("SQLiteMemory")


class SQLiteMemory(IMemoryService):
    """
    SQLite 本地记忆适配器（异步版）

    零依赖回退方案，支持：
    - 消息存储
    - FTS5 全文搜索
    - 简单语义匹配（基于关键词）
    - 记忆总结
    """

    def __init__(self, db_path: str = "./data/elysia_memory.sqlite"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        self._conn: Optional[aiosqlite.Connection] = None
        self._init_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()  # 单连接串行化保护（aiosqlite 连接非并发安全）
        self._write_count = 0  # 写操作计数，用于触发 checkpoint

    async def _ensure_init(self):
        """延迟初始化数据库（协程安全）

        创建并持有单持久连接，设置 WAL + synchronous=NORMAL，
        完成建表、FTS5 虚拟表、触发器与索引。
        """
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return

            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL：崩溃安全性与 FULL 几乎等价，但 commit 快约 100 倍（Windows fsync 慢）
            await conn.execute("PRAGMA synchronous=NORMAL")
            # 使用 SQLite 默认 wal_autocheckpoint（1000 页 = 4MB）。
            # 切勿设为 20 等小值：FTS5+多索引的 INSERT 会产生大量 WAL 页，
            # 每次 commit 都触发自动 checkpoint（Windows 上 fsync 主库 ~200ms），
            # 导致高频写极慢。WAL 回收由 _maybe_checkpoint 手动 TRUNCATE 控制。
            await conn.execute("PRAGMA wal_autocheckpoint=1000")

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT,
                    content TEXT NOT NULL,
                    msg_type TEXT DEFAULT 'text',
                    tags TEXT,
                    importance REAL DEFAULT 0.5,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
                    content, user_id, group_id, tags,
                    content_rowid=rowid
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TRIGGER IF NOT EXISTS msg_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO msg_fts(rowid, content, user_id, group_id, tags)
                    VALUES (new.id, new.content, new.user_id, new.group_id, new.tags);
                END
            """)

            # 索引创建（幂等，兼容老数据库升级）
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_user_created ON messages(user_id, created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_user_imp_created ON messages(user_id, importance, created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_group ON messages(group_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_summary_user_level ON summaries(user_id, level, created_at)")

            await conn.commit()

            self._conn = conn
            self._initialized = True

    async def _maybe_checkpoint(self, force: bool = False):
        """写操作后定期执行 WAL checkpoint（单连接下安全）

        双阈值（v2.5.0）：写计数 >= 20 常规截断；WAL 超过主库大小立即截断
        （FTS5 自动合并会产生 WAL 峰值，防止文件膨胀）。
        """
        self._write_count += 1
        if force or self._write_count >= 20 or self._wal_too_big():
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._write_count = 0
            except Exception as e:
                logger.debug(f"checkpoint failed (non-fatal): {e}")

    def _wal_too_big(self) -> bool:
        """WAL 超过主库大小且超过 256KB（FTS5 合并峰值防护，避免 DB 早期误触发）"""
        try:
            wal = os.path.getsize(str(self.db_path) + "-wal")
            if wal <= 262144:  # 256KB 绝对下限
                return False
            db = os.path.getsize(str(self.db_path))
            return db > 0 and wal > db
        except OSError:
            return False

    async def _maybe_read_checkpoint(self):
        """读操作后尝试 WAL checkpoint（不增加写计数）"""
        if self._write_count >= 20:
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._write_count = 0
            except Exception as e:
                logger.debug(f"read checkpoint failed (non-fatal): {e}")

    @staticmethod
    def _escape_fts5(query: str) -> str:
        """转义 FTS5 特殊字符，防止语法错误"""
        # FTS5 特殊字符: " * ( ) - OR AND NOT
        # 策略：将查询用双引号包裹，内部的双引号转义为两个双引号
        query = query.strip()
        if not query:
            return ""
        escaped = query.replace('"', '""')
        return f'"{escaped}"'

    async def retrieve(
        self, user_id: str, query: str, group_id: Optional[str] = None,
        include_recent: int = 3, include_semantic: int = 3,
        include_profile: bool = True, include_active: bool = False,
        include_emotion: bool = False, include_tool_history: bool = False,
        **filters
    ) -> Dict[str, Any]:
        """检索记忆（异步版）

        Returns:
            {
                "profile": str,
                "recent": List[dict],
                "semantic": List[dict],
                "active": List[dict],
                "emotion_history": str,
                "tool_history": List[dict],
            }
        """
        await self._ensure_init()
        result = {
            "profile": "",
            "recent": [],
            "semantic": [],
            "active": [],
            "emotion_history": "",
            "tool_history": [],
        }

        async with self._op_lock:
            conn = self._conn

            # --- recent ---
            if include_recent:
                if group_id:
                    sql = "SELECT * FROM messages WHERE user_id = ? AND group_id = ? ORDER BY created_at DESC LIMIT ?"
                    params = (user_id, group_id, include_recent)
                else:
                    sql = "SELECT * FROM messages WHERE user_id = ? AND group_id IS NULL ORDER BY created_at DESC LIMIT ?"
                    params = (user_id, include_recent)
                async with conn.execute(sql, params) as cursor:
                    rows = await cursor.fetchall()
                    result["recent"] = [dict(r) for r in rows]

            # --- semantic (FTS5) ---
            if include_semantic and query:
                safe_query = self._escape_fts5(query)
                if group_id:
                    sql = (
                        "SELECT * FROM messages WHERE id IN "
                        "(SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?) "
                        "AND user_id = ? AND group_id = ? LIMIT ?"
                    )
                    params = (safe_query, user_id, group_id, include_semantic)
                else:
                    sql = (
                        "SELECT * FROM messages WHERE id IN "
                        "(SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?) "
                        "AND user_id = ? AND group_id IS NULL LIMIT ?"
                    )
                    params = (safe_query, user_id, include_semantic)
                try:
                    async with conn.execute(sql, params) as cursor:
                        rows = await cursor.fetchall()
                        result["semantic"] = [dict(r) for r in rows]
                except Exception as e:
                    logger.warning(f"FTS5 semantic search failed: {e}")
                    result["semantic"] = []

            # --- profile ---
            if include_profile:
                async with conn.execute(
                    "SELECT content FROM messages WHERE user_id = ? AND importance > 0.7 ORDER BY created_at DESC LIMIT 1",
                    (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    result["profile"] = row["content"] if row else ""

            # --- active (tags 精确匹配 'active') ---
            if include_active:
                async with conn.execute(
                    "SELECT * FROM messages WHERE user_id = ? AND (tags = ? OR tags LIKE ? OR tags LIKE ? OR tags LIKE ?) ORDER BY created_at DESC LIMIT 5",
                    (user_id, "active", "active,%", "%,active", "%,active,%")
                ) as cursor:
                    rows = await cursor.fetchall()
                    result["active"] = [dict(r) for r in rows]

            # --- emotion_history (tags 精确匹配 'emotion') ---
            if include_emotion:
                async with conn.execute(
                    "SELECT content FROM messages WHERE user_id = ? AND (tags = ? OR tags LIKE ? OR tags LIKE ? OR tags LIKE ?) ORDER BY created_at DESC LIMIT 1",
                    (user_id, "emotion", "emotion,%", "%,emotion", "%,emotion,%")
                ) as cursor:
                    row = await cursor.fetchone()
                    result["emotion_history"] = row["content"] if row else ""

            # --- tool_history (tags 精确匹配 'tool') ---
            if include_tool_history:
                async with conn.execute(
                    "SELECT * FROM messages WHERE user_id = ? AND (tags = ? OR tags LIKE ? OR tags LIKE ? OR tags LIKE ?) ORDER BY created_at DESC LIMIT 5",
                    (user_id, "tool", "tool,%", "%,tool", "%,tool,%")
                ) as cursor:
                    rows = await cursor.fetchall()
                    result["tool_history"] = [dict(r) for r in rows]

            # 读操作后也尝试 checkpoint，防止长期只读导致 WAL 累积
            await self._maybe_read_checkpoint()

        return result

    async def store(
        self, user_id: str, memory_type: str, data: Dict[str, Any],
        group_id: Optional[str] = None, importance: float = 0.5,
        tags: Optional[List[str]] = None
    ) -> None:
        """存储记忆（异步版）"""
        await self._ensure_init()
        tags = list(tags or [])  # 复制列表，避免修改传入对象
        tags.append(memory_type)
        content = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)

        async with self._op_lock:
            await self._conn.execute(
                "INSERT INTO messages (user_id, group_id, content, tags, importance) VALUES (?, ?, ?, ?, ?)",
                (user_id, group_id, content, ",".join(tags), importance)
            )
            await self._conn.commit()
            await self._maybe_checkpoint()

    async def delete(self, memory_id: str) -> bool:
        await self._ensure_init()
        async with self._op_lock:
            # 先删除 FTS5 索引，避免外键/触发器逻辑错误
            await self._conn.execute("DELETE FROM msg_fts WHERE rowid = ?", (memory_id,))
            cur = await self._conn.execute("DELETE FROM messages WHERE id = ?", (memory_id,))
            await self._conn.commit()
            await self._maybe_checkpoint()
            return cur.rowcount > 0

    async def search(
        self, query: str, search_type: str = "hybrid",
        group_id: Optional[str] = None, limit: int = 10, **filters
    ) -> List[Dict[str, Any]]:
        await self._ensure_init()
        async with self._op_lock:
            conn = self._conn
            if search_type in ("fulltext", "hybrid"):
                safe_query = self._escape_fts5(query)
                if group_id:
                    sql = (
                        "SELECT * FROM messages WHERE id IN "
                        "(SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?) "
                        "AND group_id = ? LIMIT ?"
                    )
                    params = (safe_query, group_id, limit)
                else:
                    sql = (
                        "SELECT * FROM messages WHERE id IN "
                        "(SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?) "
                        "AND group_id IS NULL LIMIT ?"
                    )
                    params = (safe_query, limit)
                try:
                    async with conn.execute(sql, params) as cursor:
                        rows = await cursor.fetchall()
                except Exception as e:
                    logger.warning(f"FTS5 search failed: {e}")
                    rows = []
            else:
                if group_id:
                    sql = "SELECT * FROM messages WHERE content LIKE ? AND group_id = ? ORDER BY created_at DESC LIMIT ?"
                    params = (f"%{query}%", group_id, limit)
                else:
                    sql = "SELECT * FROM messages WHERE content LIKE ? AND group_id IS NULL ORDER BY created_at DESC LIMIT ?"
                    params = (f"%{query}%", limit)
                async with conn.execute(sql, params) as cursor:
                    rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def summarize(self, user_id: str, level: str = "24h") -> Dict[str, Any]:
        await self._ensure_init()
        async with self._op_lock:
            async with self._conn.execute(
                "SELECT * FROM summaries WHERE user_id = ? AND level = ? ORDER BY created_at DESC LIMIT 1",
                (user_id, level)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else {"summary": "", "level": level}

    async def health_check(self) -> bool:
        try:
            await self._ensure_init()
            async with self._op_lock:
                await self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        """关闭时强制 WAL checkpoint，释放磁盘空间并关闭持久连接"""
        async with self._init_lock:
            if not self._initialized or self._conn is None:
                return
            try:
                async with self._op_lock:
                    # 强制 TRUNCATE，将 WAL 内容并入主库并截断
                    await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._write_count = 0
                    await self._conn.close()
                logger.info("SQLiteMemory closed, WAL checkpointed")
            except Exception as e:
                logger.warning(f"SQLiteMemory close failed: {e}")
            finally:
                self._conn = None
                self._initialized = False
                # Windows：aiosqlite 后台线程退出有延迟，等待句柄释放
                # （否则 close 后立即删除 db 文件会 PermissionError）
                await asyncio.sleep(0.05)
