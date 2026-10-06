"""Telegram Bot 适配器"""
import re
import time
from typing import Dict, Any, Optional, List

from .message import BotMessage, MessageFormat, MessageType
from .platform_adapter import BasePlatformAdapter, PlatformType
from ..utils.logger import get_logger

logger = get_logger("TelegramAdapter")


class TelegramBotAdapter(BasePlatformAdapter):
    """
    Telegram Bot 适配器

    使用 Telegram Bot API（长轮询模式，无需公网 IP）。
    支持：私聊、群聊、图片、回复消息、@Bot 触发。

    配置示例：
        config = {
            "token": "123456:ABC-DEF...",
            "admin_ids": ["123456"],
            "trigger_mode": "at_bot",  # at_bot | all | mentioned
            "command_prefix": "/",
            "max_poll_interval": 5,
        }
    """

    DEFAULT_CONFIG = {
        "token": "",
        "admin_ids": [],
        "trigger_mode": "at_bot",
        "command_prefix": "/",
        "max_poll_interval": 5,
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._token = self._config.get("token", "")
        self._admin_ids = self._config.get("admin_ids", [])
        self._trigger_mode = self._config.get("trigger_mode", "at_bot")
        self._command_prefix = self._config.get("command_prefix", "/")
        self._max_poll_interval = self._config.get("max_poll_interval", 5)
        self._session = None
        self._last_update_id = -1
        self._bot_username = None

    @property
    def platform_type(self) -> str:
        return PlatformType.TELEGRAM.value

    @property
    def supports_multimodal(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "Telegram"

    async def start(self) -> None:
        """启动 Telegram 长轮询"""
        if not self._token:
            raise ValueError("Telegram token is required")
        await super().start()
        self._session = self._create_session()
        self._bot_username = await self._get_bot_username()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Telegram Bot started (polling, @{self._bot_username})")

    async def stop(self) -> None:
        await super().stop()
        if self._session:
            self._session.close()

    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> bool:
        """发送消息到 Telegram"""
        if not self._session:
            return False
        try:
            resp = await self._api_request(
                "sendMessage",
                {
                    "chat_id": target_id,
                    "text": content,
                    "parse_mode": "HTML",
                    "reply_to_message_id": reply_to,
                }
            )
            if resp.get("ok"):
                logger.debug(f"Sent message to {target_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
        return False

    async def send_image(self, target_id: str, image: str, caption: str = "") -> bool:
        if not self._session:
            return False
        try:
            resp = await self._api_request(
                "sendPhoto",
                {
                    "chat_id": target_id,
                    "photo": image,
                    "caption": caption,
                }
            )
            return resp.get("ok", False)
        except Exception as e:
            logger.error(f"Failed to send image: {e}")
        return False

    # ===== 消息轮询 =====

    async def _poll_loop(self):
        """长轮询消息循环"""
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Poll error: {e}")
                await asyncio.sleep(self._max_poll_interval)

    async def _handle_update(self, update: dict):
        """处理 Telegram update"""
        message = update.get("message")
        if not message:
            return

        msg = self._parse_telegram_message(message)
        if msg is None:
            return

        # 检查触发模式
        if not self._should_trigger(msg):
            return

        # 分发给处理器
        result = await self._dispatch_message(msg)
        if result:
            target = msg.group_id or msg.user_id
            await self.send_message(target, result)

    def _parse_telegram_message(self, tg_msg: dict) -> Optional[BotMessage]:
        """解析 Telegram 消息为统一格式"""
        chat = tg_msg.get("chat", {})
        from_user = tg_msg.get("from", {})
        text = tg_msg.get("text", "")
        photo = tg_msg.get("photo", [])

        msg = BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=str(tg_msg.get("message_id", "")),
            timestamp=tg_msg.get("date", time.time()),
            user_id=str(from_user.get("id", "")),
            username=from_user.get("username", ""),
            content=text,
            images=[p.get("file_unique_id", "") for p in photo] if photo else [],
            group_id=str(chat.get("id", "")) if chat.get("type", "") in ("group", "supergroup") else None,
            group_name=chat.get("title"),
            is_group=chat.get("type", "") in ("group", "supergroup"),
            reply_to=str(tg_msg["reply_to_message"]["message_id"]) if tg_msg.get("reply_to_message") else None,
        )

        # 检查是否 @Bot 或命令
        if self._bot_username and f"@{self._bot_username}" in text:
            msg.at_bot = True
            msg.content = re.sub(rf"@{re.escape(self._bot_username)}\s*", "", text).strip()
        if text and text[0] == self._command_prefix:
            msg.prefix = self._command_prefix

        return msg

    def _should_trigger(self, msg: BotMessage) -> bool:
        """检查是否应该触发 Bot 响应"""
        if self._trigger_mode == "all":
            return True
        if self._trigger_mode == "at_bot":
            return msg.at_bot or msg.is_command or not msg.is_group
        if self._trigger_mode == "mentioned":
            return msg.at_bot or msg.prefix
        return True

    # ===== API 请求（纯 aiohttp，无需 telegram-sdk）=====

    def _create_session(self):
        import aiohttp
        return aiohttp.ClientSession()

    async def _api_request(self, method: str, data: dict = None) -> dict:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        async with self._session.post(url, json=data) as resp:
            return await resp.json()

    async def _get_updates(self) -> list:
        resp = await self._api_request("getUpdates", {
            "offset": self._last_update_id + 1,
            "timeout": 5,
        })
        updates = resp.get("result", [])
        if updates:
            self._last_update_id = updates[-1].get("update_id", self._last_update_id)
        return updates

    async def _get_bot_username(self) -> str:
        try:
            resp = await self._api_request("getMe")
            user = resp.get("result", {})
            return user.get("username", "unknown")
        except Exception as e:
            logger.warning(f"Failed to get bot username: {e}")
            return "unknown"
