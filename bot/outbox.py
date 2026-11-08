"""可靠投递：write-ahead 出站队列

为什么需要它：
    平台的 ``send_message`` 是「发出去就忘」的——网络抖一下、进程正好退出，
    那条回复就永远消失了。用户看到的是「bot 没理我」，而日志里什么都没有。

这里的做法（对应 s08 Delivery）：
    1. 先落盘，再发送。``data/outbox/<message_id>.json`` 写成功之前，绝不碰网络。
    2. 发送失败按 1s→2s→4s→8s→16s 退避重试，上限 5 次。
    3. ``message_id`` 同时是幂等键：已 done 的不再重发，避免重放造成重复投递。
    4. 进程重启时扫描 pending/sending 项，接手继续发。

设计上刻意不碰各平台的实现细节：它只包住 ``send_message`` 这一个调用。
"""

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..utils.logger import get_logger

logger = get_logger("Outbox")

# 退避序列：第 n 次失败后等 BACKOFF[n] 秒
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
MAX_ATTEMPTS = len(BACKOFF_SECONDS)
# 已完成条目保留时长，供对账；超时清理
DONE_RETENTION_SECONDS = 24 * 3600


@dataclass
class OutboxItem:
    """一条待投递消息"""

    message_id: str
    platform: str
    target_id: str
    content: str = ""
    images: Optional[List[Any]] = None
    reply_to: Optional[str] = None
    kwargs: Dict[str, Any] = field(default_factory=dict)
    state: str = "pending"          # pending | sending | done | failed
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_error: str = ""

    @classmethod
    def new(cls, platform: str, target_id: str, content: str, images=None,
            reply_to=None, kwargs=None) -> "OutboxItem":
        return cls(
            message_id=uuid.uuid4().hex,
            platform=platform,
            target_id=target_id,
            content=content,
            images=images,
            reply_to=reply_to,
            kwargs=kwargs or {},
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "OutboxItem":
        data = json.loads(raw)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class OutboxStore:
    """消息落盘存储（一条一个文件，原子写）"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, message_id: str) -> Path:
        # message_id 由本模块生成（uuid4.hex），但仍做一次白名单校验：
        # 这个字段有可能来自恢复出来的旧文件，不能让它拼出目录穿越的路径。
        # 这里选择「拒绝」而不是「剔掉非法字符」：净化会让 ../../a 和 a 落到
        # 同一个文件名上，把两封不同的信塞进同一个信封，比报错更难查。
        safe = "".join(c for c in message_id if c.isalnum())
        if not safe or safe != message_id:
            raise ValueError(f"非法 message_id: {message_id!r}")
        return self.root / f"{safe}.json"

    def put(self, item: OutboxItem) -> None:
        """落盘（先写临时文件再替换，断电不会留下半截 JSON）"""
        item.updated_at = time.time()
        target = self._path(item.message_id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(item.to_json(), encoding="utf-8")
        os.replace(tmp, target)

    def get(self, message_id: str) -> Optional[OutboxItem]:
        path = self._path(message_id)
        if not path.exists():
            return None
        try:
            return OutboxItem.from_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"出站队列条目损坏，忽略 {path.name}: {e}")
            return None

    def all(self) -> List[OutboxItem]:
        items: List[OutboxItem] = []
        for path in self.root.glob("*.json"):
            try:
                items.append(OutboxItem.from_json(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"出站队列条目损坏，忽略 {path.name}: {e}")
        items.sort(key=lambda i: i.created_at)
        return items

    def pending(self) -> List[OutboxItem]:
        """待投递项：含「上次发送到一半就挂了」的 sending"""
        return [i for i in self.all() if i.state in ("pending", "sending")]

    def purge_done(self, retention: float = DONE_RETENTION_SECONDS) -> int:
        """清理过期的已完成条目，返回条数"""
        cutoff = time.time() - retention
        removed = 0
        for item in self.all():
            if item.state in ("done", "failed") and item.updated_at < cutoff:
                try:
                    self._path(item.message_id).unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


class ReliableSender:
    """
    可靠投递包装器

    用法：包装一个平台的 send_message，之后照常调用即可——
    返回值语义不变（True/False），只是多了一层「先落盘、失败重试」。
    """

    def __init__(self, adapter: Any, store: OutboxStore,
                 backoff: tuple = BACKOFF_SECONDS):
        self._adapter = adapter
        self._store = store
        self._backoff = backoff
        self._send: Optional[Callable] = None

    def bind(self, send_func: Callable) -> "ReliableSender":
        """绑定原始发送函数"""
        self._send = send_func
        return self

    @property
    def store(self) -> OutboxStore:
        return self._store

    async def send_message(self, target_id: str, content: str,
                           images: Optional[list] = None,
                           reply_to: Optional[str] = None,
                           **kwargs) -> bool:
        """对外接口，签名与 BasePlatformAdapter.send_message 一致"""
        item = OutboxItem.new(
            platform=getattr(self._adapter, "platform_type", "custom"),
            target_id=target_id, content=content, images=images,
            reply_to=reply_to, kwargs=kwargs,
        )
        return await self.deliver(item)

    async def deliver(self, item: OutboxItem) -> bool:
        """write-ahead → 发送 → 按结果落状态"""
        existing = self._store.get(item.message_id)
        if existing and existing.state == "done":
            # 幂等：已经发成功的，绝不再发一次
            return True

        # 1. 先落盘。这一步失败就直接放弃投递——宁可没发，也不要「发了但没记录」
        try:
            self._store.put(item)
        except OSError as e:
            logger.error(f"出站队列落盘失败，消息未发送: {e}")
            return False

        # 2. 再发送
        if self._send is None:
            logger.error("ReliableSender 未绑定发送函数")
            return False

        while True:
            item.state = "sending"
            item.attempts += 1
            try:
                self._store.put(item)
            except OSError:
                pass  # 状态记不上不影响发送本身

            try:
                ok = await self._send(
                    target_id=item.target_id, content=item.content,
                    images=item.images, reply_to=item.reply_to, **item.kwargs,
                )
            except Exception as e:
                ok = False
                item.last_error = f"{type(e).__name__}: {e}"

            if ok:
                item.state = "done"
                item.last_error = ""
                try:
                    self._store.put(item)
                except OSError:
                    pass
                return True

            if item.attempts >= MAX_ATTEMPTS:
                item.state = "failed"
                try:
                    self._store.put(item)
                except OSError:
                    pass
                logger.error(
                    f"投递失败（已重试 {item.attempts} 次），放弃: "
                    f"{item.platform}/{item.target_id} {item.last_error}"
                )
                return False

            delay = self._backoff[min(item.attempts - 1, len(self._backoff) - 1)]
            item.state = "pending"
            try:
                self._store.put(item)
            except OSError:
                pass
            logger.warning(
                f"投递失败，{delay}s 后重试（{item.attempts}/{MAX_ATTEMPTS}）: "
                f"{item.last_error or '平台返回 False'}"
            )
            await asyncio.sleep(delay)

    async def recover(self) -> int:
        """重启后重放未完成项，返回成功条数"""
        # 顺手做一次过期清理：done 条目只为对账保留 DONE_RETENTION_SECONDS，
        # 而进程平时没有别的时机去删，不在这里清就会只增不减。
        try:
            purged = self._store.purge_done()
            if purged:
                logger.info(f"出站队列清理过期已完成条目: {purged} 条")
        except OSError as e:
            # 清理失败不影响重放：留着文件只是占盘，不会重复投递
            logger.warning(f"出站队列过期清理失败（忽略）: {e}")

        pending = self._store.pending()
        if not pending:
            return 0
        logger.info(f"出站队列恢复：{len(pending)} 条待投递")
        ok_count = 0
        for item in pending:
            if await self.deliver(item):
                ok_count += 1
        logger.info(f"出站队列恢复完成：{ok_count}/{len(pending)}")
        return ok_count


def install(adapter: Any, root: Path, name: str = "") -> ReliableSender:
    """
    给适配器装上可靠投递

    直接替换实例上的 send_message。因为各平台子类内部也是
    ``await self.send_message(...)``，替换后所有出站调用都会经过队列，
    不用改动任何一个适配器实现。
    """
    sub = Path(root) / (name or getattr(adapter, "platform_type", "custom"))
    sender = ReliableSender(adapter, OutboxStore(sub))
    sender.bind(adapter.send_message)
    adapter.send_message = sender.send_message  # type: ignore[assignment]
    adapter._outbox = sender                # type: ignore[attr-defined]
    return sender
