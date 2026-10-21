"""QQ 官方机器人适配器 - 接入 QQ 开放平台（QQ 频道 / QQ 群机器人）

接入方式：
- 通过 WebSocket 长连接接收实时消息事件（wss://api.sgroup.qq.com/websocket）
- 通过开放平台 HTTP API（base_url + AppID + AppSecret 签名）发送消息

支持：
- 私聊 / 群聊 / 频道消息
- 图片 / 富文本（markdown）消息
- 回复消息
- 开放平台签名鉴权（Authorization: Bot <AppID>.<AppToken>）

配置示例：
    config = {
        "app_id": "102XXXXXX",          # 开放平台 AppID
        "app_secret": "xxxx",           # 开放平台 AppSecret（用于签名）
        "app_token": "xxxx",            # 开放平台 AppToken（用于发送鉴权）
        "base_url": "https://api.sgroup.qq.com",   # 可选，默认官方网关
        "intents": ["GROUP_AND_C2C_EVENT"],         # 可选，订阅的事件
        "sandbox": True,                 # 是否沙箱环境
    }
"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .message import BotMessage, MessageFormat
from .platform_adapter import BasePlatformAdapter, PlatformType
from ..utils.logger import get_logger
from ..utils.task_guard import spawn_background_task

logger = get_logger("QQOfficialAdapter")

# QQ 官方机器人网关
QQ_OFFICIAL_WS = "wss://api.sgroup.qq.com/websocket"
QQ_OFFICIAL_BASE = "https://api.sgroup.qq.com"


class QQOfficialBotAdapter(BasePlatformAdapter):
    """
    QQ 官方机器人适配器

    基于 QQ 开放平台，通过 WebSocket 接收事件 + HTTP API 发送消息。
    """

    DEFAULT_CONFIG = {
        "app_id": "",
        "app_secret": "",
        "app_token": "",
        "base_url": QQ_OFFICIAL_BASE,
        "intents": ["GROUP_AND_C2C_EVENT"],
        "sandbox": True,
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._app_id = self._config.get("app_id", "")
        self._app_secret = self._config.get("app_secret", "")
        self._app_token = self._config.get("app_token", "")
        self._base_url = self._config.get("base_url", QQ_OFFICIAL_BASE)
        self._intents = self._config.get("intents", ["GROUP_AND_C2C_EVENT"])
        self._sandbox = self._config.get("sandbox", True)
        self._ws = None
        self._session = None
        self._seq = 0

    # ===== 平台标识 =====

    @property
    def platform_type(self) -> str:
        return PlatformType.QQ.value

    @property
    def supports_multimodal(self) -> bool:
        return True

    @property
    def supports_group(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "QQOfficial"

    @property
    def _intent_bit(self) -> int:
        """QQ 官方 intent 位运算（简化映射）"""
        # 0x01 GUILDS, 0x20 GROUP_AND_C2C_EVENT, 0x100 DIRECT_MESSAGE
        bit = 0
        for intent in self._intents:
            if intent == "GUILDS":
                bit |= 0x01
            elif intent == "GROUP_AND_C2C_EVENT":
                bit |= 0x20
            elif intent == "DIRECT_MESSAGE":
                bit |= 0x100
            elif intent == "GUILD_MESSAGES":
                bit |= 0x400
        return bit

    # ===== 生命周期 =====

    async def start(self) -> None:
        """启动 QQ 官方机器人：建立 WebSocket 长连接"""
        if not self._app_id:
            raise ValueError("QQ Official app_id is required")

        try:
            import aiohttp
        except ImportError:
            raise ImportError(
                "aiohttp is required for QQ official adapter. "
                "Install: pip install aiohttp"
            )

        await super().start()

        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._ws_loop())
        logger.info(f"QQ Official adapter connecting as AppID={self._app_id}")

    async def stop(self) -> None:
        await super().stop()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("QQ Official adapter stopped")

    # ===== WebSocket 事件循环 =====

    async def _ws_loop(self) -> None:
        """WebSocket 主循环：鉴权 + 收消息"""
        import aiohttp

        headers = {
            "Authorization": f"Bot {self._app_id}.{self._app_token}",
        }

        while self._running:
            try:
                async with self._session.ws_connect(
                    QQ_OFFICIAL_WS, headers=headers, heartbeat=30
                ) as ws:
                    self._ws = ws
                    logger.info("QQ Official WebSocket connected")

                    # 发送鉴权（IDENTIFY）
                    identify = {
                        "op": 2,
                        "d": {
                            "token": f"{self._app_id}.{self._app_token}",
                            "intents": self._intent_bit,
                            "shard": [0, 1],
                        },
                    }
                    await ws.send_json(identify)

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await self._handle_ws_event(data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"QQ WS error: {ws.exception()}")
                            break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"QQ WS loop error: {e}")
                await asyncio.sleep(3)  # 重连延迟

    async def _handle_ws_event(self, data: Dict[str, Any]) -> None:
        """处理 WS 事件帧"""
        op = data.get("op")
        d = data.get("d", {})
        t = data.get("t")

        # HELLO
        if op == 10:
            interval = d.get("heartbeat_interval", 41250) / 1000.0
            self._heartbeat_task = asyncio.create_task(self._heartbeat(interval))
        # HEARTBEAT_ACK
        elif op == 11:
            pass
        # READY / RESUMED
        elif op == 0:
            self._handle_dispatch(t, d)

    async def _heartbeat(self, interval: float) -> None:
        """发送心跳保活"""
        while self._running and self._ws and not self._ws.closed:
            try:
                await self._ws.send_json({"op": 1, "d": self._seq})
            except Exception as e:
                logger.warning(f"QQ heartbeat failed: {e}")
                return
            await asyncio.sleep(interval)

    def _handle_dispatch(self, t: str, d: Dict[str, Any]) -> None:
        """分发消息事件"""
        if t in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            msg = self._parse_official_message(d)
            if msg:
                spawn_background_task(
                    self._deliver(msg),
                    name="qq-deliver",
                )

    async def _deliver(self, msg: BotMessage) -> None:
        """投递消息到处理器"""
        try:
            result = await self._dispatch_message(msg)
            if result:
                await self.send_message(
                    target_id=msg.group_id or msg.user_id,
                    content=result,
                    reply_to=msg.message_id,
                    is_group=msg.is_group,
                )
        except Exception as e:
            logger.error(f"QQ deliver error: {e}")

    # ===== 消息发送 =====

    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        is_group: bool = False,
        **kwargs
    ) -> bool:
        """通过开放平台 API 发送消息"""
        if not self._session:
            return False

        # 发送端点：群聊/私聊
        if is_group:
            url = f"{self._base_url}/v2/groups/{target_id}/messages"
        else:
            url = f"{self._base_url}/v2/users/{target_id}/messages"

        payload: Dict[str, Any] = {}
        if reply_to:
            payload["msg_id"] = reply_to

        # 消息类型：文本 / 图片 / 富文本
        if images:
            # QQ 官方富文本图片语法：<fileurl>...</fileurl>
            text_part = f"{content} " if content else ""
            payload["content"] = text_part + f"<fileurl>{images[0]}</fileurl>"
            payload["msg_type"] = 2  # 富媒体
        else:
            payload["content"] = content[:2000]
            payload["msg_type"] = 0  # 文本

        headers = {
            "Authorization": f"Bot {self._app_id}.{self._app_token}",
            "Content-Type": "application/json",
        }

        try:
            async with self._session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    self._seq = self._seq + 1
                    return True
                body = await resp.text()
                logger.error(f"QQ send failed [{resp.status}]: {body}")
                return False
        except Exception as e:
            logger.error(f"QQ send error: {e}")
            return False

    # ===== 消息解析 =====

    def _parse_official_message(self, d: Dict[str, Any]) -> Optional[BotMessage]:
        """解析 QQ 官方消息事件为统一 BotMessage"""
        author = d.get("author", {})
        group_openid = d.get("group_openid")
        user_openid = d.get("author", {}).get("user_openid")
        content = d.get("content", "").strip()

        msg = BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=str(d.get("id", "")),
            timestamp=time.time(),
            user_id=user_openid or str(author.get("id", "")),
            username=author.get("member_openid") or author.get("username", "") or user_openid or "",
            content=content,
            group_id=group_openid,
            is_group=bool(group_openid),
            at_bot=True,  # 群消息事件均为 @机器人
        )

        # 解析图片附件
        attachments = d.get("attachments") or []
        for att in attachments:
            if att.get("content_type", "").startswith("image"):
                msg.images.append(att.get("url", ""))

        # 解析富文本 markdown
        if "ark" in d or "message_reference" in d:
            msg.content_type = MessageFormat.MARKDOWN

        return msg

    # ===== 健康检查 =====

    async def health_check(self) -> bool:
        return self._running and bool(self._ws) and not self._ws.closed