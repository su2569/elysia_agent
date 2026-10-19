"""OneBot v11 反向 WebSocket 适配器（收发合一）

连接专用 WS 服务（服务端上报事件，客户端通过同一连接调用 API），
实现 QQ 群/私聊消息的收发。

协议（OneBot v11 反向 WS）：
- 连接: ws://host:port  (Authorization: Bearer <token>)
- 事件: {"post_type":"message","message_type":"group","group_id":..,"user_id":..,
         "message":[...],"raw_message":"..","self_id":..}
- API:  发送 {"action":"send_group_msg","params":{...},"echo":"id"}，
         服务端回 {"status":"ok","retcode":0,"data":{...},"echo":"id"}

安全：仅处理白名单群（allowed_groups）内的消息，其余忽略。
"""
import asyncio
import json
import time
import uuid
from typing import Dict, Any, Optional

from .message import BotMessage, MessageFormat, MessageType
from .platform_adapter import BasePlatformAdapter, PlatformType
from ..utils.logger import get_logger

logger = get_logger("OneBotWS")


class OneBotWSAdapter(BasePlatformAdapter):
    """
    OneBot v11 反向 WebSocket 适配器

    配置示例：
        config = {
            "host": "127.0.0.1",
            "port": 1005,
            "token": "20261111----1005",
            "allowed_groups": ["1049279188"],   # 仅处理这些群，空=全部
            "auto_reconnect": True,
            "reconnect_interval": 5,
        }
    """

    DEFAULT_CONFIG = {
        "host": "127.0.0.1",
        "port": 1005,
        "token": "",
        "allowed_groups": [],
        "auto_reconnect": True,
        "reconnect_interval": 5,
        # 触发与过滤（测试阶段）
        "trigger_word": "♪",
        "at_trigger": False,
        "quote_trigger": False,
        "permission_roles": ["owner", "admin"],
        "permission_enabled": True,
        "repeat_ignore_enabled": True,
        "repeat_ignore_window_sec": 60,
        "repeat_ignore_count": 3,
        "bot_ignore_enabled": True,
    }

    # 机器人 user_id 标识（OneBot 上报 sender.user_id 命中则忽略，或 sender.bot == True）
    BOT_IDS = {
        "2854196310",
        "2854196306",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._host = self._config.get("host", "127.0.0.1")
        self._port = int(self._config.get("port", 1005))
        self._token = self._config.get("token", "")
        self._allowed_groups = set(str(g) for g in self._config.get("allowed_groups", []))
        self._auto_reconnect = self._config.get("auto_reconnect", True)
        self._reconnect_interval = float(self._config.get("reconnect_interval", 5))
        self._ws = None
        self._session = None
        self._pending: Dict[str, asyncio.Future] = {}  # echo -> future
        self._self_id = ""

        # 触发与过滤（测试阶段）
        self._trigger_word = str(self._config.get("trigger_word", "♪"))
        self._at_trigger = bool(self._config.get("at_trigger", False))
        self._quote_trigger = bool(self._config.get("quote_trigger", False))
        self._permission_roles = set(str(r) for r in self._config.get("permission_roles", ["owner", "admin"]))
        self._permission_enabled = bool(self._config.get("permission_enabled", True))
        self._repeat_ignore_enabled = bool(self._config.get("repeat_ignore_enabled", True))
        self._repeat_window_sec = float(self._config.get("repeat_ignore_window_sec", 60))
        self._repeat_count = int(self._config.get("repeat_ignore_count", 3))
        self._bot_ignore_enabled = bool(self._config.get("bot_ignore_enabled", True))

        # 复读机跟踪：key = (group_id, user_id, text) -> [时间戳...]
        self._repeat_history: Dict[tuple, list] = {}

    @property
    def platform_type(self) -> str:
        return "onebot_ws"

    @property
    def name(self) -> str:
        return "OneBotWS"

    @property
    def supports_group(self) -> bool:
        return True

    @property
    def supports_multimodal(self) -> bool:
        return True

    def ws_url(self) -> str:
        return f"ws://{self._host}:{self._port}"

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """连接 WS 服务并启动接收循环"""
        await super().start()
        self._session = self._create_session()
        await self._connect()
        self._task = asyncio.create_task(self._receive_loop())
        logger.info(f"OneBotWS started: {self.ws_url()} (token={'***' if self._token else 'none'})")

    async def stop(self) -> None:
        await super().stop()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session:
            await self._session.close()
        # 清理挂起的 API 调用
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("adapter stopped"))
        self._pending.clear()

    def _create_session(self):
        import aiohttp
        return aiohttp.ClientSession()

    async def _connect(self) -> None:
        """建立 WS 连接（带鉴权）"""
        import aiohttp
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            self._ws = await self._session.ws_connect(
                self.ws_url(), headers=headers, heartbeat=30, timeout=30
            )
            logger.info(f"OneBotWS connected: {self.ws_url()}")
        except Exception as e:
            logger.error(f"OneBotWS connect failed: {e}")
            self._ws = None
            raise

    async def _receive_loop(self) -> None:
        """接收事件循环（自动重连）"""
        import aiohttp
        while self._running:
            if self._ws is None or self._ws.closed:
                if not self._auto_reconnect:
                    break
                try:
                    await self._connect()
                except Exception:
                    await asyncio.sleep(self._reconnect_interval)
                    continue
            try:
                msg = await self._ws.receive()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_payload(json.loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"OneBotWS connection closed: {msg.extra if hasattr(msg, 'extra') else ''}")
                    self._ws = None
                    if self._auto_reconnect:
                        await asyncio.sleep(self._reconnect_interval)
                    else:
                        break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"OneBotWS receive error: {e}")
                self._ws = None
                if self._auto_reconnect:
                    await asyncio.sleep(self._reconnect_interval)
                else:
                    break

    async def _handle_payload(self, payload: dict) -> None:
        """处理 WS 消息：API 响应（有 echo）或事件（post_type）"""
        if "echo" in payload:
            # API 调用响应
            fut = self._pending.pop(str(payload.get("echo")), None)
            if fut and not fut.done():
                fut.set_result(payload)
            return
        if payload.get("post_type") == "message":
            await self._handle_message_event(payload)
        elif payload.get("post_type") == "meta_event":
            if payload.get("meta_event_type") == "lifecycle":
                self._self_id = str(payload.get("self_id", ""))
                logger.info(f"OneBotWS lifecycle: {payload.get('sub_type', 'connect')}")

    async def _handle_message_event(self, evt: dict) -> None:
        """处理消息事件（群白名单过滤）"""
        msg_type = evt.get("message_type", "")
        group_id = str(evt.get("group_id", ""))
        user_id = str(evt.get("user_id", ""))

        # 仅允许白名单群
        if self._allowed_groups and msg_type == "group" and group_id not in self._allowed_groups:
            logger.debug(f"Ignore message from non-whitelisted group: {group_id}")
            return

        # 机器人忽略
        if self._bot_ignore_enabled and self._is_bot(evt):
            logger.debug(f"Ignore bot message from user_id={user_id}")
            return

        # 复读机忽略：1 分钟内连续 N 次几乎相同内容
        if self._repeat_ignore_enabled and msg_type == "group":
            raw_text = self._extract_text(evt.get("message", evt.get("raw_message", "")))
            if self._is_repeat_spam(group_id, user_id, raw_text):
                logger.debug(f"Ignore repeat spam from group={group_id} user={user_id}")
                return

        # 提取纯文本与图片
        content, images = self._extract_text_and_images(evt.get("message", evt.get("raw_message", "")))

        # 触发判定：♪ 触发词 / 艾特触发 / 引用触发
        at_bot = self._is_at_bot(evt)
        trigger_by_word = bool(self._trigger_word) and content.lstrip().startswith(self._trigger_word)
        trigger_by_at = bool(self._at_trigger) and at_bot
        # 引用触发：消息含 reply 段（引用上一条）
        trigger_by_quote = bool(self._quote_trigger) and self._is_quote(evt)

        # 私聊默认视为触发
        if msg_type == "private":
            triggered = True
        else:
            triggered = trigger_by_word or trigger_by_at or trigger_by_quote

        if not triggered:
            return

        # 权限控制：仅群主/群管理员有权限（普通成员仅可被触发词/@ 带起）
        if self._permission_enabled and msg_type == "group" and not self._has_permission(evt):
            if not (trigger_by_at or trigger_by_word):
                logger.debug(f"No permission for group={group_id} user={user_id} role={self._sender_role(evt)}")
                return

        # 剥离触发词前缀（♪）
        if trigger_by_word and content.lstrip().startswith(self._trigger_word):
            content = content.lstrip()[len(self._trigger_word):].lstrip()

        bot_msg = BotMessage(
            platform=self.platform_type,
            platform_type=self.platform_type,
            message_id=str(evt.get("message_id", "")),
            timestamp=time.time(),
            user_id=user_id,
            content=content,
            images=images,
            group_id=group_id if msg_type == "group" else None,
            is_group=msg_type == "group",
            at_bot=at_bot,
            raw=evt,
        )

        result = await self._dispatch_message(bot_msg)
        if result:
            await self.send_message(bot_msg.group_id or bot_msg.user_id, result,
                                    reply_to=bot_msg.message_id)

    def _extract_text(self, message) -> str:
        """从 OneBot 消息段提取纯文本"""
        text, _ = self._extract_text_and_images(message)
        return text

    def _extract_text_and_images(self, message):
        """提取纯文本与图片 URL 列表"""
        if isinstance(message, str):
            return message, []
        parts = []
        images = []
        if isinstance(message, list):
            for seg in message:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        parts.append(seg.get("data", {}).get("text", ""))
                    elif seg.get("type") == "image":
                        parts.append("[图片]")
                        f = seg.get("data", {}).get("file", "") or seg.get("data", {}).get("url", "")
                        if f:
                            images.append(f)
                    elif seg.get("type") == "at":
                        parts.append(f"@{seg.get('data', {}).get('qq', '')}")
                    else:
                        parts.append(f"[{seg.get('type', 'msg')}]")
        return "".join(parts), images

    def _is_at_bot(self, evt: dict) -> bool:
        """检查是否 @了 Bot"""
        msg = evt.get("message", [])
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str(seg.get("data", {}).get("qq", ""))
                    if qq == self._self_id or qq == "0":
                        return True
        return False

    def _is_quote(self, evt: dict) -> bool:
        """检查消息是否引用了上一条（reply 段）"""
        msg = evt.get("message", [])
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    return True
        return False

    def _sender_role(self, evt: dict) -> str:
        """获取发送者群角色：owner/admin/member"""
        sender = evt.get("sender", {}) or {}
        return str(sender.get("role", "") or "member").lower()

    def _has_permission(self, evt: dict) -> bool:
        """是否具备权限（群主/群管理员）"""
        return self._sender_role(evt) in self._permission_roles

    def _is_bot(self, evt: dict) -> bool:
        """机器人判断：sender.bot == True（bool bot）或 sender.user_id 命中机器人标识"""
        sender = evt.get("sender", {}) or {}
        # OneBot 上报的 bool bot 字段
        if bool(sender.get("bot")):
            return True
        if str(sender.get("user_id", "")) in self.BOT_IDS:
            return True
        if str(evt.get("user_id", "")) in self.BOT_IDS:
            return True
        return False

    def _is_repeat_spam(self, group_id: str, user_id: str, text: str) -> bool:
        """复读机判断：1 分钟内同用户连续发 N 次几乎相同内容"""
        if not text:
            return False
        now = time.time()
        key = (group_id, user_id, text)
        hist = self._repeat_history.setdefault(key, [])
        # 清掉超过时间窗的旧记录
        hist[:] = [t for t in hist if now - t <= self._repeat_window_sec]
        hist.append(now)
        if len(hist) >= self._repeat_count:
            return True
        # 清理其它 key 防止内存膨胀（上限 5000）
        # 只删过期条目不够——若攻击者用大量不同的新消息刷屏，全部都在窗口内，
        # 字典会无限增长。因此超限时先淘汰最久未活动的 key。
        if len(self._repeat_history) > 5000:
            # 1) 先丢弃过期条目
            live = {k: v for k, v in self._repeat_history.items()
                    if v and now - v[-1] <= self._repeat_window_sec}
            # 2) 仍超限则按最近一次活动时间淘汰最旧的
            if len(live) > 5000:
                ordered = sorted(live.items(), key=lambda kv: kv[1][-1])
                self._repeat_history = dict(ordered[-(5000 - 1):])  # 留 4999 + 当前 key
            else:
                self._repeat_history = live
        return False

    # ==================== 发送 ====================

    async def send_message(
        self,
        target_id: str,
        content: str,
        images: Optional[list] = None,
        reply_to: Optional[str] = None,
        **kwargs
    ) -> bool:
        """通过 WS 调用 OneBot API 发送消息"""
        if not self._ws or self._ws.closed:
            logger.warning("OneBotWS not connected, cannot send")
            return False

        if kwargs.get("private"):
            action = "send_private_msg"
            params: Dict[str, Any] = {"user_id": int(target_id), "message": content}
        else:
            action = "send_group_msg"
            params = {"group_id": int(target_id), "message": content}

        if reply_to:
            params["reply"] = reply_to

        try:
            resp = await self.call_api(action, params, timeout=10)
            return bool(resp and resp.get("status") == "ok")
        except Exception as e:
            logger.error(f"OneBotWS send failed: {e}")
            return False

    async def call_api(self, action: str, params: Dict[str, Any],
                       timeout: float = 10.0) -> Optional[Dict[str, Any]]:
        """调用 OneBot API（echo 匹配请求-响应）"""
        if not self._ws or self._ws.closed:
            return None
        echo = uuid.uuid4().hex[:16]
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_json({"action": action, "params": params, "echo": echo})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            logger.warning(f"OneBotWS API timeout: {action}")
            return None
        except Exception as e:
            self._pending.pop(echo, None)
            logger.error(f"OneBotWS API error: {action}: {e}")
            return None

    # ==================== 健康检查 ====================

    async def health_check(self) -> bool:
        return bool(self._ws and not self._ws.closed) and self._running