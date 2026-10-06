"""平台适配器基类 - 统一 Bot 平台接入"""
import asyncio
import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, Dict, Any, Optional

from ..core.context import Message
from ..utils.logger import get_logger
from .message import BotMessage

logger = get_logger("PlatformAdapter")


class PlatformType(str, Enum):
    """支持的平台类型"""
    TELEGRAM = "telegram"
    DISCORD = "discord"
    HTTP_WEBHOOK = "http"
    WECHAT = "wechat"
    QQ = "qq"
    CUSTOM = "custom"


class BasePlatformAdapter(ABC):
    """
    平台适配器抽象基类

    所有 Bot 平台适配器继承此类，实现统一的：
    - start/stop 生命周期
    - 消息收发（send_message / on_message）
    - 健康检查
    - 平台特有配置

    设计参考：AstrBot 的 PlatformAdapter 抽象层
    """

    # 子类覆盖
    DEFAULT_CONFIG: Dict[str, Any] = {}

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._running = False
        self._message_handlers: list = []
        self._task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        """适配器名称"""
        return type(self).__name__

    @property
    def platform_type(self) -> str:
        """平台类型标识"""
        return PlatformType.CUSTOM.value

    @property
    def running(self) -> bool:
        return self._running

    @property
    def config(self) -> Dict[str, Any]:
        return self._config

    @property
    def supports_multimodal(self) -> bool:
        """是否支持多模态（图片/音频/视频）"""
        return False

    @property
    def supports_group(self) -> bool:
        """是否支持群聊"""
        return True

    # ===== 生命周期 =====

    @abstractmethod
    async def start(self) -> None:
        """启动适配器（开始监听平台消息）"""
        self._running = True

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ===== 消息收发 =====

    @abstractmethod
    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> bool:
        """
        发送消息到平台

        Args:
            target_id: 目标用户或群组 ID
            content: 消息文本内容
            images: 图片列表（URL 或 base64）
            reply_to: 回复的消息 ID
        """
        pass

    def on_message(self, handler: Callable) -> Callable:
        """
        注册消息处理器

        Args:
            handler: 消息处理函数 (async def handler(msg: BotMessage) -> Optional[str])
        """
        self._message_handlers.append(handler)
        return handler

    # ===== 内部消息分发 =====

    async def _dispatch_message(self, msg: BotMessage) -> Optional[str]:
        """
        分发消息到已注册的处理器

        返回 Bot 回复内容（由调用方决定如何发送）
        """
        for handler in self._message_handlers:
            try:
                result = handler(msg)
                if asyncio.iscoroutinefunction(handler):
                    result = await result
                if result is not None:
                    return result
            except Exception as e:
                logger.error(f"Message handler error: {e}")
        return None

    # ===== 健康检查 =====

    async def health_check(self) -> bool:
        """检查平台连接状态"""
        return self._running

    # ===== 日志 =====

    def log(self, level: str, message: str):
        logger.info(f"[{self.platform_type}] {message}")

    def __repr__(self):
        return f"<{self.name} platform={self.platform_type} running={self._running}>"
