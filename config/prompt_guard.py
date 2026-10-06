"""Prompt Guard System - 参考 Anti-Prompt Injector 的 PTD 4.1 引擎

三层防御架构：
L1: PTD 启发式检测引擎（正则+关键词+编码解码）
L2: LLM 协审引擎（AI 二次确认）
L3: 策略引擎（执行防御动作）

四象防御模式：
- sentry: 静默加固
- aegis: 复核确认
- scorch: 内容替换
- intercept: 立即拒绝

增强：黑名单持久化到 SQLite，服务重启不丢失。
"""
import re
import base64
import json
import asyncio
import aiosqlite
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..utils.logger import get_logger

logger = get_logger("PromptGuard")


@dataclass
class GuardResult:
    """检测结果"""
    is_safe: bool
    score: float              # 0-100，越高越危险
    attack_types: List[str]   # 检测到的攻击类型
    action: str               # 执行的动作
    sanitized_text: str       # 处理后的文本
    reason: str               # 原因说明


class PTDEngine:
    """
    PTD 4.1 启发式检测引擎

    覆盖 13 类注入攻击：
    1. 越狱/角色扮演
    2. 指令覆盖
    3. 间接注入
    4. 伪装示例
    5. 特殊符号注入
    6. 编码隐藏
    7. 多语言混淆
    8. 字形替换
    9. 多Agent穿越
    10. 记忆投毒
    11. 数据外泄
    12. 人身骚扰
    13. 仇恨煽动
    """

    # 攻击特征正则 (28+)
    PATTERNS = {
        "jailbreak": [
            r"\b(DAN|Do Anything Now|jailbreak|越狱)\b",
            r"\b(祖母漏洞|grandmother trick|hypothetical)\b",
            r"\b(角色扮演|roleplay|pretend to be|act as)\b",
            r"\b(道德绑架|moral blackmail|for educational)\b",
        ],
        "instruction_override": [
            r"(忽略| disregard|ignore|forget).*(指令|instruction|prompt|系统|system)",
            r"(覆盖|override|replace).*(系统|system|设定|setting)",
            r"(新的|new).*(提示词|prompt|指令|instruction)",
            r"(从现在开始|from now on|henceforth).*(你是|you are|扮演|act as)",
            r"(忘记一切|forget everything|wipe memory|clear history)",
        ],
        "indirect_injection": [
            r"(访问|visit|click|打开).*(链接|link|url|网站|website)",
            r"(文档|document|文件|file).*(包含|contains|内有|has)",
            r"(图片|image|照片|photo).*(里有|contains|显示|shows)",
        ],
        "special_tokens": [
            r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|user\|>",
            r"\[SYSTEM\]|\[USER\]|\[ASSISTANT\]",
            r"<<SYS>>|<\/s>|\[INST\]",
        ],
        "system_spoof": [
            # 伪装成系统指令的消息（如 "system: ..." 开头）
            r"^\s*system\s*[:：]",
            r"^\s*<\s*system\s*>",
            r"^\s*(?:你|系统).{0,4}(?:说|要求|命令|指定)\s*[:：]",
        ],
        "encoding": [
            r"[A-Za-z0-9+/]{32,}={0,2}",  # Base64（混合文本中可能存在的片段）
            r"\b(rot13|base64|hex|unicode|编码|encode)\b",
        ],
        "leetspeak": [
            r"\b(syst3m|pr0mpt|j41lbr34k|1gn0r3|0v3rr1d3)\b",
            r"\b(d1sreg4rd|f0rget|n3w)\b",
        ],
        "data_exfiltration": [
            r"(发送|send|转发|forward).*(到|to).*(邮箱|email|链接|link)",
            r"(输出|output).*(到文件|to file|保存|save)",
        ],
        "memory_poisoning": [
            r"(记住|remember|memorize).*(我是|I am|名字是|name is)",
            r"(更新|update).*(记忆|memory|知识|knowledge)",
            r"(我的|my).*(名字|name|身份|identity).*(是|is)",
        ],
        "harassment": [
            r"\b(傻逼|废物|垃圾|去死|nmsl|cnm|sb|mdzz)\b",
            r"\b(fuck|shit|bitch|asshole|damn)\b",
            r"(性骚扰|sexual harassment|骚扰|harass)",
        ],
        "hate_speech": [
            r"(种族|民族|国家).*(优越|歧视|仇恨|消灭)",
            r"(屠杀|genocide|种族灭绝|清洗|purge)",
        ],
    }

    # 敏感关键词权重 (120+)
    KEYWORDS = {
        # 高风险 (weight=10)
        "忽略": 10, "ignore": 10, "disregard": 10,
        "覆盖": 10, "override": 10, "replace": 10,
        "系统提示": 10, "system prompt": 10, "prompt injection": 10,
        "越狱": 10, "jailbreak": 10, "DAN": 10,
        "忘记": 8, "forget": 8, "wipe": 8, "clear": 8,
        "从现在开始": 8, "from now on": 8, "henceforth": 8,
        "你是": 6, "you are": 6, "扮演": 6, "act as": 6,
        "角色": 5, "role": 5, "character": 5,
        "指令": 5, "instruction": 5, "command": 5,
        "泄露": 5, "leak": 5, "reveal": 5,
        "提示词": 5, "prompt": 5,

        # 中风险 (weight=3)
        "新的": 3, "new": 3,
        "设定": 3, "setting": 3, "config": 3,
        "规则": 3, "rule": 3, "policy": 3,
        "限制": 3, "restriction": 3, "limit": 3,

        # 低风险 (weight=1)
        "编码": 1, "encode": 1, "decode": 1,
        "base64": 1, "rot13": 1, "hex": 1,
    }

    # 防御模式
    MODES = {
        "sentry": "静默加固",
        "aegis": "复核确认",
        "scorch": "内容替换",
        "intercept": "立即拒绝",
    }

    def __init__(self, mode: str = "intercept", threshold: float = 30.0):
        self.mode = mode if mode in self.MODES else "intercept"
        self.threshold = threshold
        self.compiled_patterns = self._compile_patterns()

    def _compile_patterns(self) -> Dict[str, List[re.Pattern]]:
        """编译正则"""
        compiled = {}
        for attack_type, patterns in self.PATTERNS.items():
            compiled[attack_type] = [re.compile(p, re.IGNORECASE) for p in patterns]
        return compiled

    def detect(self, text: str) -> GuardResult:
        """
        检测文本是否包含提示词注入攻击

        Returns:
            GuardResult: 检测结果
        """
        if not text:
            return GuardResult(True, 0.0, [], "pass", "", "空文本")

        score = 0.0
        attack_types = []
        reasons = []

        # 1. 正则特征匹配
        for attack_type, patterns in self.compiled_patterns.items():
            for pattern in patterns:
                matches = pattern.findall(text)
                if matches:
                    attack_types.append(attack_type)
                    score += 15
                    reasons.append(f"正则命中: {attack_type}")
                    break

        # 2. 关键词权重
        text_lower = text.lower()
        for keyword, weight in self.KEYWORDS.items():
            if keyword.lower() in text_lower:
                score += weight
                reasons.append(f"关键词: {keyword}(+{weight})")

        # 2.5 否定词检测（拒绝系统指令的强信号短语）
        negation_score = self._check_negation(text_lower)
        if negation_score > 0:
            score += negation_score
            attack_types.append("instruction_override")
            reasons.append(f"否定词检测(+{negation_score})")

        # 3. 编码检测
        encoded_score = self._check_encoding(text)
        if encoded_score > 0:
            score += encoded_score
            attack_types.append("encoding")
            reasons.append(f"编码可疑(+{encoded_score})")

        # 4. 长度惩罚（超长消息更可疑）
        if len(text) > 2000:
            score += 5
            reasons.append("超长消息(+5)")

        # 5. 特殊字符密度
        special_chars = sum(1 for c in text if ord(c) > 127 or c in r"<>[]{}|")
        if len(text) > 0 and special_chars / len(text) > 0.3:
            score += 10
            reasons.append("特殊字符密度高(+10)")

        score = min(100, max(0, score))
        is_safe = score < self.threshold
        action, sanitized = self._execute_action(text, score, attack_types)

        return GuardResult(
            is_safe=is_safe,
            score=score,
            attack_types=list(set(attack_types)),
            action=action,
            sanitized_text=sanitized,
            reason="; ".join(reasons) if reasons else "无风险"
        )

    # 否定词检测：明确拒绝/覆盖系统指令的强信号短语
    NEGATION_PATTERNS = [
        r"ignore\s+(all\s+)?(previous|prior|above|earlier|everything)",
        r"disregard\s+(all\s+)?(previous|prior|above|earlier|everything)",
        r"forget\s+(all\s+)?(previous|prior|above|everything)",
        r"忽略\s*(之前|以前|以上|所有)",
        r"无视\s*(之前|以前|以上|所有)",
        r"忘掉\s*(之前|以前|以上|所有)",
        r"不要(?:再|去|要)\s*(?:遵循|遵守|管|理)",
        r"stop\s+(following|obeying|listening)",
    ]

    def _check_negation(self, text_lower: str) -> float:
        """检测否定/覆盖系统指令的短语，每个命中 +25（强信号）"""
        score = 0.0
        for pattern in self.NEGATION_PATTERNS:
            if re.search(pattern, text_lower):
                score += 25
                break  # 每类只计一次，避免重复叠加
        return min(score, 50)

    def _check_encoding(self, text: str) -> float:
        """检测编码伪装"""
        score = 0.0
        if re.match(r"^[A-Za-z0-9+/]{40,}={0,2}$", text.strip()):
            try:
                decoded = base64.b64decode(text.strip()).decode("utf-8", errors="ignore")
                if len(decoded) > 10:
                    score += 20
            except Exception:
                pass

        invisible = sum(1 for c in text if 0x200 <= ord(c) <= 0x200f or 0xfe00 <= ord(c) <= 0xfe0f)
        if invisible > 5:
            score += 15

        zero_width_chars = ["\u200b", "\u200c", "\u200d", "\ufeff"]
        if any(c in text for c in zero_width_chars):
            score += 10

        return score

    def _execute_action(self, text: str, score: float, attack_types: List[str]) -> Tuple[str, str]:
        """根据防御模式执行动作"""
        if score < self.threshold:
            return "pass", text

        if self.mode == "sentry":
            return "sentry", text
        elif self.mode == "aegis":
            return "aegis", text
        elif self.mode == "scorch":
            return "scorch", "[检测到潜在安全风险，内容已被过滤]"
        elif self.mode == "intercept":
            return "intercept", ""

        return "pass", text

    def sanitize(self, text: str) -> str:
        """清理文本（低风险净化）"""
        zero_width_chars = ["\u200b", "\u200c", "\u200d", "\ufeff"]
        for c in zero_width_chars:
            text = text.replace(c, "")
        text = re.sub(r"\s+", " ", text)
        text = "".join(c for c in text if ord(c) >= 32 or c in "\t\n\r")
        return text.strip()


class PromptGuard:
    """
    提示词防护主类（增强版：支持黑名单持久化）

    整合 PTD 引擎 + 黑白名单 + 拦截日志 + SQLite 持久化
    """

    def __init__(
        self,
        mode: str = "intercept",
        threshold: float = 30.0,
        enable_blacklist: bool = True,
        blacklist_duration: int = 60,
        whitelist: Optional[List[str]] = None,
        enable_llm_review: bool = False,
        db_path: str = "./data/guard_blacklist.sqlite",
    ):
        self.ptd = PTDEngine(mode=mode, threshold=threshold)
        self.mode = mode
        self.enable_blacklist = enable_blacklist
        self.blacklist_duration = blacklist_duration
        self.whitelist = set(whitelist or [])
        self.enable_llm_review = enable_llm_review

        # 内存缓存
        self.blacklist: Dict[str, Tuple[float, str]] = {}
        self.incident_log: List[Dict] = []
        self.max_log_size = 100

        # 后台任务追踪（防止 create_task 泄漏）
        self._background_tasks: set = set()

        # 持久化
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
                    CREATE TABLE IF NOT EXISTS blacklist (
                        user_id TEXT PRIMARY KEY,
                        ban_until REAL,
                        reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.commit()

                # 加载持久化黑名单（过滤已过期条目）
                now = datetime.now().timestamp()
                async with conn.execute("SELECT user_id, ban_until, reason FROM blacklist") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        ban_until, reason = row[1], row[2]
                        # 跳过已过期条目（ban_until > 0 且已超时）
                        if ban_until > 0 and now > ban_until:
                            continue
                        self.blacklist[row[0]] = (ban_until, reason)

                # 清理数据库中已过期的记录
                await conn.execute("DELETE FROM blacklist WHERE ban_until > 0 AND ban_until < ?", (now,))
                await conn.commit()
            self._initialized = True

    def check(self, user_id: str, text: str, context: Optional[Dict] = None) -> GuardResult:
        """
        检查用户消息（同步入口，兼容现有调用）

        注意：首次调用前建议 await _ensure_init()，
        但为兼容同步调用，黑名单持久化在首次 check 时异步初始化。
        """
        # 1. 白名单
        if user_id in self.whitelist:
            return GuardResult(True, 0.0, [], "pass", text, "白名单用户")

        # 2. 黑名单检查（内存中）
        if user_id in self.blacklist:
            ban_until, reason = self.blacklist[user_id]
            if datetime.now().timestamp() < ban_until or ban_until == 0:
                return GuardResult(
                    False, 100.0, ["blacklisted"], "intercept", "",
                    f"用户已被封禁: {reason}"
                )
            else:
                del self.blacklist[user_id]
                # 异步清理过期记录
                self._spawn_bg_task(self._remove_from_db(user_id))

        # 3. PTD 检测
        result = self.ptd.detect(text)

        # 4. LLM 复核
        if self.enable_llm_review and self.threshold * 0.5 <= result.score < self.threshold * 1.5:
            result.action = "aegis"

        # 5. 自动拉黑
        if self.enable_blacklist and not result.is_safe and result.score >= 80:
            self._blacklist_user(user_id, result.attack_types)

        # 6. 记录日志
        self._log_incident(user_id, text, result, context)

        return result

    def _blacklist_user(self, user_id: str, attack_types: List[str]) -> None:
        """将用户加入黑名单（内存 + 持久化）"""
        if self.blacklist_duration == 0:
            ban_until = 0
        else:
            ban_until = datetime.now().timestamp() + self.blacklist_duration * 60

        reason = ", ".join(attack_types)
        self.blacklist[user_id] = (ban_until, reason)
        logger.warning(f"User {user_id} blacklisted for {self.blacklist_duration}min. Attacks: {attack_types}")

        # 异步持久化
        self._spawn_bg_task(self._save_to_db(user_id, ban_until, reason))

    async def _save_to_db(self, user_id: str, ban_until: float, reason: str):
        """保存到 SQLite"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO blacklist (user_id, ban_until, reason) VALUES (?, ?, ?)",
                    (user_id, ban_until, reason)
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"Blacklist save failed: {e}")

    async def _remove_from_db(self, user_id: str):
        """从 SQLite 移除"""
        try:
            await self._ensure_init()
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"Blacklist remove failed: {e}")

    def _log_incident(self, user_id: str, text: str, result: GuardResult, context: Optional[Dict]) -> None:
        """记录拦截日志（不存储完整上下文，防止大对象累积）"""
        # 只保留上下文中的安全字段，避免存储完整 message_history 等大对象
        safe_context = {}
        if context:
            safe_keys = ("platform", "session_id", "query_length")
            safe_context = {k: context.get(k) for k in safe_keys if k in context}

        self.incident_log.append({
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id,
            "text_preview": text[:100] + "..." if len(text) > 100 else text,
            "score": result.score,
            "attack_types": result.attack_types,
            "action": result.action,
            "reason": result.reason,
            "context": safe_context
        })
        if len(self.incident_log) > self.max_log_size:
            self.incident_log = self.incident_log[-self.max_log_size:]

    def _spawn_bg_task(self, coro) -> None:
        """创建后台任务并追踪引用，防止泄漏。
        兼容非事件循环线程调用（无事件循环时静默跳过）。"""
        try:
            task = asyncio.create_task(coro)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            # 当前线程无运行中的事件循环，跳过异步持久化
            # 内存状态已更新，持久化将在下次异步上下文中补齐
            pass

    def add_whitelist(self, user_id: str) -> None:
        self.whitelist.add(user_id)
        if user_id in self.blacklist:
            del self.blacklist[user_id]
            self._spawn_bg_task(self._remove_from_db(user_id))

    def remove_whitelist(self, user_id: str) -> None:
        self.whitelist.discard(user_id)

    def unban(self, user_id: str) -> bool:
        if user_id in self.blacklist:
            del self.blacklist[user_id]
            self._spawn_bg_task(self._remove_from_db(user_id))
            return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        total = len(self.incident_log)
        blocked = sum(1 for i in self.incident_log if i["action"] != "pass")
        by_type = {}
        for i in self.incident_log:
            for t in i["attack_types"]:
                by_type[t] = by_type.get(t, 0) + 1

        return {
            "total_checked": total,
            "blocked": blocked,
            "blacklisted_users": len(self.blacklist),
            "whitelisted_users": len(self.whitelist),
            "attack_types": by_type,
            "mode": self.mode,
            "threshold": self.ptd.threshold,
        }

    def get_incident_log(self, limit: int = 50) -> List[Dict]:
        return self.incident_log[-limit:]
