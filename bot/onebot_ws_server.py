"""OneBot v11 正向 WebSocket 适配器（服务端模式）

Bot 作为 WS 服务端监听，QQ 官方机器人框架（如 NapCat/LLOneBot 等）反向连接进来。
与反向 WS（客户端模式）互补：反向 = bot 连服务；正向 = 服务等 bot 连接。

协议（OneBot v11 正向 WS）：
- 监听: ws://0.0.0.0:port
- 鉴权: 连接时校验 Authorization: Bearer <token>（或 access_token 参数）
- 无 token 连接仅允许本地回路（127.0.0.1），外部必须带有效 token
- 事件: bot 向服务端发送 {"post_type":"message",...}（服务端再分发到 runner）
- API:  服务端发 {"action":"send_group_msg","params":{...},"echo":"id"}，
        客户端回 {"status":"ok","retcode":0,"data":{...},"echo":"id"}

安全：仅处理白名单群（allowed_groups），token 校验失败立即断开。
"""
import asyncio
import json
import time
import uuid
from typing import Dict, Any, Optional, List

import aiohttp
from aiohttp import web

from .message import BotMessage
from .platform_adapter import BasePlatformAdapter
from ..utils.logger import get_logger

logger = get_logger("OneBotWSServer")


class OneBotWSServerAdapter(BasePlatformAdapter):
    """
    OneBot v11 正向 WebSocket 适配器（服务端）

    配置示例：
        config = {
            "host": "0.0.0.0",
            "port": 1006,
            "token": "20261111----1005",
            "allowed_groups": ["1049279188"],
            "path": "/ws",            # 连接路径
        }
    """

    DEFAULT_CONFIG = {
        "host": "0.0.0.0",
        "port": 1006,
        "token": "",
        "allowed_groups": [],
        "path": "/ws",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._host = self._config.get("host", "0.0.0.0")
        self._port = int(self._config.get("port", 1006))
        self._token = self._config.get("token", "")
        self._allowed_groups = set(str(g) for g in self._config.get("allowed_groups", []))
        self._path = self._config.get("path", "/ws")
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._clients: Dict[int, web.WebSocketResponse] = {}  # id -> ws
        self._pending_api: Dict[str, asyncio.Future] = {}     # echo -> future
        self._self_id = ""

    @property
    def platform_type(self) -> str:
        return "onebot_ws_server"

    @property
    def name(self) -> str:
        return "OneBotWSServer"

    @property
    def supports_group(self) -> bool:
        return True

    @property
    def supports_multimodal(self) -> bool:
        return True

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动 WS 服务端"""
        await super().start()
        self._app = web.Application()
        self._app.router.add_get(self._path, self._handle_ws)
        self._app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info(f"OneBotWSServer started: ws://{self._host}:{self._port}{self._path}")

    async def stop(self) -> None:
        await super().stop()
        for ws in list(self._clients.values()):
            if not ws.closed:
                await ws.close()
        self._clients.clear()
        if self._runner:
            await self._runner.cleanup()

    async def health_check(self) -> bool:
        return bool(self._clients)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok", "running": self._running,
            "clients": len(self._clients),
        })

    # ==================== WS 连接处理 ====================

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """接受 QQ 框架连接（鉴权：无 token 仅本地回路）"""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        # 鉴权：Authorization: Bearer token 或 access_token 查询参数
        auth = request.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "").strip() if auth else request.query.get("access_token", "")

        # 本地回路检查：无 token（或 token 不匹配）时仅允许本地连接
        remote = (request.remote or "").split(":")[0]
        is_loopback = remote in ("127.0.0.1", "::1", "localhost") or remote.startswith("127.")

        if self._token:
            if token == self._token:
                logger.info(f"OneBotWSServer: 客户端鉴权通过 ({remote})")
            elif is_loopback:
                logger.info(f"OneBotWSServer: 本地回路无 token 连接放行 ({remote})")
            else:
                logger.warning(f"OneBotWSServer: 鉴权失败（非本地且无有效 token），拒绝 ({request.remote})")
                await ws.close(code=4401, message=b"Unauthorized")
                return ws
        else:
            # 未配置 token：仅允许本地回路连接
            if not is_loopback:
                logger.warning(f"OneBotWSServer: 未配置 token，拒绝非本地连接 ({request.remote})")
                await ws.close(code=4401, message=b"Forbidden")
                return ws

        client_id = id(ws)
        self._clients[client_id] = ws
        logger.info(f"OneBotWSServer: 客户端已连接 ({request.remote}), 共 {len(self._clients)} 个")

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    # 客户端发来的消息事件
                    if payload.get("post_type") == "message":
                        await self._handle_message_event(payload)
                    elif payload.get("post_type") == "meta_event":
                        if payload.get("meta_event_type") == "lifecycle":
                            self._self_id = str(payload.get("self_id", ""))
                    # API 调用响应（echo 匹配）
                    elif "echo" in payload:
                        await self._resolve_api_response(payload)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"OneBotWSServer ws error: {e}")
        finally:
            self._clients.pop(client_id, None)
            logger.info(f"OneBotWSServer: 客户端断开, 剩余 {len(self._clients)} 个")
        return ws

    # ==================== 消息处理 ====================

    async def _handle_message_event(self, evt: dict) -> None:
        msg_type = evt.get("message_type", "")
        group_id = str(evt.get("group_id", ""))
        if self._allowed_groups and msg_type == "group" and group_id not in self._allowed_groups:
            logger.debug(f"Ignore message from non-whitelisted group: {group_id}")
            return

        content = self._extract_text(evt.get("message", evt.get("raw_message", "")))
        bot_msg = BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=str(evt.get("message_id", "")),
            timestamp=time.time(),
            user_id=str(evt.get("user_id", "")),
            content=content,
            group_id=group_id if msg_type == "group" else None,
            is_group=msg_type == "group",
            at_bot=self._is_at_bot(evt),
            raw=evt,
        )
        result = await self._dispatch_message(bot_msg)
        if result:
            await self.send_message(bot_msg.group_id or bot_msg.user_id, result,
                                    reply_to=bot_msg.message_id)

    def _extract_text(self, message) -> str:
        if isinstance(message, str):
            return message
        parts = []
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    t = seg.get("type")
                    if t == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif t == "image":
                        parts.append("[图片]")
                    elif t == "at":
                        parts.append(f"@{seg.get('data', {}).get('qq', '')}")
                    else:
                        parts.append(f"[{t}]")
        return "".join(parts)

    def _is_at_bot(self, evt: dict) -> bool:
        msg = evt.get("message", [])
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str(seg.get("data", {}).get("qq", ""))
                    if qq == self._self_id or qq == "0":
                        return True
        return False

    # ==================== 发送（API 调用） ====================

    async def send_message(self, target_id: str, content: str, images: Optional[list] = None,
                           reply_to: Optional[str] = None, **kwargs) -> bool:
        if not self._clients:
            logger.warning("OneBotWSServer: 无客户端连接，无法发送")
            return False
        if kwargs.get("private"):
            action, params = "send_private_msg", {"user_id": int(target_id), "message": content}
        else:
            action, params = "send_group_msg", {"group_id": int(target_id), "message": content}
        if reply_to:
            params["reply"] = reply_to
        resp = await self.call_api(action, params)
        return bool(resp and resp.get("status") == "ok")

    async def call_api(self, action: str, params: Dict[str, Any],
                       timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """向第一个可用客户端调用 API"""
        for ws in list(self._clients.values()):
            if ws.closed:
                continue
            echo = uuid.uuid4().hex[:16]
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending_api[echo] = fut
            try:
                await ws.send_json({"action": action, "params": params, "echo": echo})
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending_api.pop(echo, None)
                logger.warning(f"OneBotWSServer API timeout: {action}")
                return None
            except Exception as e:
                self._pending_api.pop(echo, None)
                logger.error(f"OneBotWSServer API error: {e}")
                return None
        return None

    async def _resolve_api_response(self, payload: dict) -> None:
        """处理客户端回传的 API 响应"""
        fut = self._pending_api.pop(str(payload.get("echo", "")), None)
        if fut and not fut.done():
            fut.set_result(payload)