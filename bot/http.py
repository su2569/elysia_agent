"""HTTP Webhook 适配器 - 通过 HTTP API 接收消息"""
import json
import time
from typing import Dict, Any, Optional

import aiohttp.web
from aiohttp import web

from .message import BotMessage, MessageFormat, MessageType
from .platform_adapter import BasePlatformAdapter, PlatformType
from ..utils.logger import get_logger

logger = get_logger("HTTPWebhookAdapter")


class HTTPWebhookAdapter(BasePlatformAdapter):
    """
    HTTP Webhook 适配器

    通过 HTTP POST 接收消息，返回 Bot 回复。
    适用于：自定义前端、Webhook 回调、HTTP API 网关后端。

    接收格式：
        POST /webhook
        {
            "user_id": "user1",
            "username": "Alice",
            "group_id": "group1",  // 可选
            "content": "你好",
            "images": ["url"],     // 可选
            "reply_to": "msg123",  // 可选
        }

    返回格式：
        {
            "text": "回复内容",
            "images": ["url"],
            "success": true,
        }

    配置示例：
        config = {
            "host": "0.0.0.0",
            "port": 8080,
            "auth_token": "secret",  # 可选，验证请求
            "path": "/webhook",      # 可选，默认 /webhook
        }
    """

    DEFAULT_CONFIG = {
        "host": "0.0.0.0",
        "port": 8080,
        "auth_token": "",
        "path": "/webhook",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._host = self._config.get("host", "0.0.0.0")
        self._port = self._config.get("port", 8080)
        self._auth_token = self._config.get("auth_token", "")
        self._path = self._config.get("path", "/webhook")
        self._app: Optional[aiohttp.web.Application] = None
        self._runner: Optional[aiohttp.web.AppRunner] = None
        self._site: Optional[aiohttp.web.TCPSite] = None

    @property
    def platform_type(self) -> str:
        return PlatformType.HTTP_WEBHOOK.value

    @property
    def name(self) -> str:
        return "HTTPWebhook"

    async def start(self) -> None:
        """启动 HTTP 服务"""
        await super().start()
        self._app = web.Application()
        self._app.router.add_post(self._path, self._handle_webhook)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(f"HTTP Webhook started at http://{self._host}:{self._port}{self._path}")

    async def stop(self) -> None:
        await super().stop()
        if self._runner:
            await self._runner.cleanup()

    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> bool:
        """
        HTTP 适配器不主动发消息，回复通过 webhook 响应返回。
        如果需要在外部推送，可以配置回调 URL。
        """
        callback_url = self._config.get("callback_url")
        if callback_url:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    await session.post(callback_url, json={
                        "target_id": target_id,
                        "content": content,
                        "images": images,
                    })
                return True
            except Exception as e:
                logger.error(f"Callback failed: {e}")
                return False
        return False

    # ===== HTTP 路由处理 =====

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """处理 webhook 请求"""
        # 验证 auth token
        if self._auth_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {self._auth_token}" and auth != self._auth_token:
                return web.json_response(
                    {"error": "Unauthorized"},
                    status=401
                )

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": "Invalid JSON"},
                status=400
            )

        # 解析消息
        msg = self._parse_webhook_message(data, request)

        # 分发
        result = await self._dispatch_message(msg)

        return web.json_response({
            "text": result or "",
            "success": result is not None,
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "running": self._running})

    def _parse_webhook_message(self, data: dict, request: web.Request) -> BotMessage:
        """解析 webhook 消息"""
        return BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=data.get("message_id", ""),
            timestamp=data.get("timestamp", time.time()),
            user_id=data.get("user_id", ""),
            username=data.get("username", ""),
            content=data.get("content", ""),
            images=data.get("images", []),
            group_id=data.get("group_id"),
            group_name=data.get("group_name"),
            is_group=bool(data.get("group_id")),
            reply_to=data.get("reply_to"),
            at_bot=data.get("at_bot", False),
            prefix=data.get("prefix", ""),
        )
