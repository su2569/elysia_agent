"""情绪引擎 - 参考MaiBot的情绪系统

增强：心情值持久化到 SQLite，服务重启不丢失。
"""
import asyncio
import json
import aiosqlite
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path

from ..core.state import EmotionState
from ..config.settings import EmotionConfig
from ..utils.logger import get_logger

logger = get_logger("EmotionEngine")


class EmotionEngine:
    """
    情绪引擎（增强版：支持心情值持久化）

    功能：
    - 从文本/图片检测情绪
    - 维护用户心情值（0.0~1.0）
    - 情绪历史记录
    - 人格对情绪的影响
    """

    def __init__(self, config: Optional[EmotionConfig] = None, db_path: str = "./data/emotion_cache.sqlite"):
        self.config = config or EmotionConfig()
        self._mood_cache: Dict[str, Dict[str, Any]] = {}  # 内存缓存
        self._max_cache_users = 1000  # 最大缓存用户数，防止内存无限增长
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
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS mood_cache (
                        user_id TEXT PRIMARY KEY,
                        score REAL DEFAULT 0.5,
                        last_update TEXT,
                        history TEXT  -- JSON
                    )
                """)
                await conn.commit()

                # 加载持久化数据
                async with conn.execute("SELECT user_id, score, last_update, history FROM mood_cache") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        self._mood_cache[row[0]] = {
                            "score": row[1],
                            "last_update": datetime.fromisoformat(row[2]),
                            "history": json.loads(row[3]) if row[3] else []
                        }
            self._initialized = True

    async def detect(self, text: str, images: Optional[list] = None) -> EmotionState:
        """检测文本情绪"""
        if not self.config.auto_detect:
            return EmotionState.NEUTRAL

        text_lower = text.lower()

        # 增强：检测否定词，减少误报
        negation_words = {"不", "没", "别", "无", "未", "否", "not", "no", "don't", "doesn't", "didn't", "never"}
        has_negation = any(w in text_lower for w in negation_words)

        emotion_map = {
            EmotionState.HAPPY: ["开心", "高兴", "哈哈", "棒", "好耶", "love", "happy", "😊", "🎉"],
            EmotionState.SAD: ["难过", "伤心", "哭", "呜呜", "sad", "😢", "😭"],
            EmotionState.ANGRY: ["生气", "愤怒", "讨厌", "烦", "angry", "😠", "🤬"],
            EmotionState.SURPRISED: ["惊讶", "哇", "居然", "真的吗", "wow", "😲", "😮"],
            EmotionState.CURIOUS: ["好奇", "为什么", "怎么", "什么", "?", "？", "🤔"],
            EmotionState.EXCITED: ["兴奋", "激动", "期待", "冲", "excited", "🤩", "🔥"],
            EmotionState.ANXIOUS: ["焦虑", "担心", "害怕", "紧张", "anxious", "😰"],
            EmotionState.LOVING: ["喜欢", "爱", "抱抱", "亲亲", "love", "❤️", "😘"],
        }

        scores = {emotion: 0 for emotion in EmotionState}
        for emotion, keywords in emotion_map.items():
            for kw in keywords:
                if kw in text_lower:
                    scores[emotion] += 1

        if max(scores.values()) == 0:
            return EmotionState.NEUTRAL

        detected = max(scores, key=scores.get)

        # 否定词反转情绪
        if has_negation and detected in (EmotionState.HAPPY, EmotionState.EXCITED, EmotionState.LOVING):
            # "不开心" -> SAD, "不喜欢" -> NEUTRAL
            return EmotionState.SAD if detected == EmotionState.HAPPY else EmotionState.NEUTRAL

        return detected

    async def get_mood_score(self, user_id: str) -> float:
        """获取用户心情值（带持久化）"""
        await self._ensure_init()

        if user_id not in self._mood_cache:
            self._mood_cache[user_id] = {
                "score": 0.5,
                "last_update": datetime.now(),
                "history": []
            }

        cache = self._mood_cache[user_id]
        # 心情自然衰减
        elapsed = (datetime.now() - cache["last_update"]).total_seconds() / 3600
        decay = elapsed * self.config.mood_decay_rate
        cache["score"] = max(0.0, min(1.0, cache["score"] - decay))
        cache["last_update"] = datetime.now()

        # 内存优化：超过最大缓存用户数时，清理最久未更新的用户
        if len(self._mood_cache) > self._max_cache_users:
            oldest_user = min(
                self._mood_cache.keys(),
                key=lambda uid: self._mood_cache[uid]["last_update"]
            )
            del self._mood_cache[oldest_user]

        return cache["score"]

    async def update(self, user_id: str, response: str, detected_emotion: EmotionState) -> None:
        """更新用户心情（带持久化）"""
        await self._ensure_init()

        if user_id not in self._mood_cache:
            await self.get_mood_score(user_id)

        cache = self._mood_cache[user_id]

        if detected_emotion in (EmotionState.HAPPY, EmotionState.EXCITED, EmotionState.LOVING):
            cache["score"] = min(1.0, cache["score"] + self.config.mood_boost_reply)
        elif detected_emotion in (EmotionState.ANGRY, EmotionState.SAD, EmotionState.ANXIOUS):
            cache["score"] = max(0.0, cache["score"] + self.config.mood_penalty_ignore)

        cache["history"].append({
            "emotion": detected_emotion.value,
            "score": cache["score"],
            "timestamp": datetime.now().isoformat()
        })

        if len(cache["history"]) > self.config.emotion_history_limit:
            cache["history"] = cache["history"][-self.config.emotion_history_limit:]

        # 持久化
        await self._persist(user_id)

    async def _persist(self, user_id: str):
        """持久化心情数据到 SQLite"""
        try:
            cache = self._mood_cache.get(user_id)
            if not cache:
                return
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO mood_cache (user_id, score, last_update, history) VALUES (?, ?, ?, ?)",
                    (user_id, cache["score"], cache["last_update"].isoformat(), json.dumps(cache["history"]))
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"Emotion persist failed: {e}")

    def get_emotion_history(self, user_id: str) -> list:
        return self._mood_cache.get(user_id, {}).get("history", [])
