"""ElysiaBot - 产品层：平台适配 + 消息路由 + API 网关"""

from .message import BotMessage, MessageFormat
from .platform_adapter import BasePlatformAdapter, PlatformType
from .telegram import TelegramBotAdapter
from .discord import DiscordBotAdapter
from .http import HTTPWebhookAdapter

__all__ = [
    "BotMessage",
    "MessageFormat",
    "BasePlatformAdapter",
    "PlatformType",
    "TelegramBotAdapter",
    "DiscordBotAdapter",
    "HTTPWebhookAdapter",
]
