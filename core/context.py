"""Elysia Agent Loop - 全量上下文"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime
import uuid

from .state import LoopState, MessageType, EmotionState

@dataclass
class Message:
    """统一消息格式（参考AstrBot的MessageChain）"""
    role: str = "user"           # system / user / assistant / tool
    content: str = ""
    msg_type: MessageType = MessageType.TEXT
    images: List[str] = field(default_factory=list)
    audio: Optional[bytes] = None
    video: Optional[str] = None
    file: Optional[str] = None
    at_target: Optional[str] = None
    reply_to: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

@dataclass
class AgentContext:
    """Agent全量上下文 - 黑匣子记录器"""
    # === 基础标识 ===
    user_id: str = ""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    group_id: Optional[str] = None
    platform: str = "unknown"    # qq / wechat / discord / web

    # === 输入 ===
    query: str = ""
    messages: List[Message] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    audio: Optional[bytes] = None

    # === 运行控制 ===
    max_steps: int = 30
    step_count: int = 0
    state: LoopState = LoopState.IDLE

    # === 被动记忆（自动捕获） ===
    thought_chain: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[Dict] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    message_history: List[Dict] = field(default_factory=list)

    # === 注入快照（双注入引擎） ===
    injected_pre: Dict[str, Any] = field(default_factory=dict)   # 注入点A：宏观背景
    injected_post: Dict[str, Any] = field(default_factory=dict)  # 注入点B：微观经验

    # === 人格与情绪（参考MaiBot） ===
    emotion: EmotionState = EmotionState.NEUTRAL
    mood_score: float = 0.5       # 0.0~1.0
    personality_override: Optional[Dict] = None

    # === 中间结果 ===
    llm_raw_response: Optional[str] = None
    tool_results: List[Dict] = field(default_factory=list)

    # === 最终结果 ===
    final_answer: str = ""
    final_messages: List[Message] = field(default_factory=list)
    error: Optional[str] = None

    # === 元数据 ===
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None

    def add_thought(self, step: int, thought: str, action: Optional[str] = None):
        """记录思考链"""
        self.thought_chain.append({
            "step": step,
            "thought": thought,
            "action": action,
            "timestamp": datetime.now().isoformat()
        })

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "group_id": self.group_id,
            "platform": self.platform,
            "query": self.query,
            "state": self.state.value,
            "step_count": self.step_count,
            "thought_chain": self.thought_chain,
            "tool_calls": self.tool_calls,
            "final_answer": self.final_answer,
            "emotion": self.emotion.value,
            "mood_score": self.mood_score,
            "error": self.error,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
        }
