"""Bot 统一消息模型 - 跨平台消息标准化"""
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional


class MessageFormat(Enum):
    """消息格式"""
    TEXT = "text"
    MARKDOWN = "markdown"
    HTML = "html"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    EMBEDDED = "embedded"  # 富文本嵌入消息
    REPLY = "reply"  # 回复消息


class MessageType(Enum):
    """消息类型"""
    INCOMING = "incoming"
    OUTGOING = "outgoing"
    SYSTEM = "system"
    COMMAND = "command"  # @命令消息


@dataclass
class BotMessage:
    """
    统一 Bot 消息模型

    所有平台的消息转换为统一格式后交给 ElysiaRunner 处理。
    Runner 的输出再转换回对应平台格式发送。
    """

    # 平台标识
    platform: str = ""
    platform_type: str = ""

    # 消息元数据
    message_id: str = ""
    timestamp: float = 0.0
    raw: Optional[Dict[str, Any]] = None  # 原始平台消息对象

    # 发送者
    user_id: str = ""
    username: str = ""
    avatar_url: Optional[str] = None

    # 目标/群组
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    is_group: bool = False

    # 内容
    content: str = ""
    content_type: MessageFormat = MessageFormat.TEXT
    images: List[str] = field(default_factory=list)  # image URLs / base64
    audio: Optional[bytes] = None
    files: List[str] = field(default_factory=list)  # file URLs / paths

    # 引用消息（回复）
    reply_to: Optional[str] = None  # 被回复的消息 ID
    quote: Optional[Dict[str, Any]] = None  # 被引用的消息内容

    # 状态
    direction: MessageType = MessageType.INCOMING
    at_bot: bool = False  # 是否在群组中 @了 Bot
    prefix: str = ""  # 命令前缀（如 / ! @）

    def __post_init__(self):
        if not self.message_id:
            self.message_id = str(uuid.uuid4())[:12]
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def is_command(self) -> bool:
        """是否为命令消息"""
        return self.prefix in ("/", "!", "#") or self.content_type == MessageType.COMMAND

    @property
    def command_name(self) -> str:
        """提取命令名称"""
        if not self.is_command:
            return ""
        parts = self.content.strip().split(None, 1)
        return parts[0].lstrip("/!#") if parts else ""

    @property
    def command_args(self) -> str:
        """提取命令参数"""
        if not self.is_command or not self.is_command:
            return ""
        parts = self.content.strip().split(None, 1)
        return parts[1] if len(parts) > 1 else ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "user_id": self.user_id,
            "username": self.username,
            "group_id": self.group_id,
            "content": self.content,
            "content_type": self.content_type.value,
            "images": self.images,
            "reply_to": self.reply_to,
            "is_group": self.is_group,
            "at_bot": self.at_bot,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotMessage":
        data = dict(data)
        content_type = data.get("content_type", "text")
        try:
            data["content_type"] = MessageFormat(content_type)
        except ValueError:
            pass
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def digest(cls, content: str) -> str:
        """消息内容摘要（用于去重）"""
        return hashlib.md5(content.encode()).hexdigest()[:16]
