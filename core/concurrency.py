"""Concurrency Module - 并发处理基础设施

提供：
- 并发安全的限流器（asyncio.Lock 保护）
- 原子统计计数器
- 全局会话并发控制（Semaphore）
- 批量 LLM 调用
- 并行工具执行
- 异步记忆操作队列
"""
import asyncio
import time
from typing import Dict, Any, Optional, List, Callable, Coroutine
from dataclasses import dataclass, field
from collections import defaultdict

from ..utils.logger import get_logger

logger = get_logger("Concurrency")


class AtomicCounter:
    """原子计数器（asyncio-safe）"""
    def __init__(self, initial: int = 0):
        self._value = initial
        self._lock = asyncio.Lock()

    async def increment(self, delta: int = 1) -> int:
        async with self._lock:
            self._value += delta
            return self._value

    async def decrement(self, delta: int = 1) -> int:
        async with self._lock:
            self._value -= delta
            return self._value

    async def get(self) -> int:
        async with self._lock:
            return self._value

    def get_sync(self) -> int:
        """同步读取（仅用于非竞争场景）"""
        return self._value


class ConcurrentRateLimiter:
    """
    并发安全的令牌桶限流器

    支持：
    - 每用户独立限流
    - 滑动窗口
    -  burst 容量
    """
    def __init__(self, max_requests: int = 30, window_seconds: int = 60, burst: int = 5):
        self.max_requests = max_requests
        self.window = window_seconds
        self.burst = burst
        self._buckets: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()
        self._cleanup_counter = 0  # 全局清理计数器

    async def check(self, user_id: str) -> bool:
        """检查是否允许通过，同时记录本次请求"""
        async with self._lock:
            now = time.time()

            # 定期全局清理不活跃用户的 bucket（每 100 次请求触发一次）
            self._cleanup_counter += 1
            if self._cleanup_counter >= 100:
                self._cleanup_counter = 0
                expired_users = [
                    uid for uid, timestamps in self._buckets.items()
                    if not timestamps or all(now - t >= self.window for t in timestamps)
                ]
                for uid in expired_users:
                    del self._buckets[uid]

            if user_id not in self._buckets:
                self._buckets[user_id] = []
            # 清理过期请求
            self._buckets[user_id] = [t for t in self._buckets[user_id] if now - t < self.window]
            # 检查是否超过限制（burst 额外允许）
            if len(self._buckets[user_id]) >= self.max_requests + self.burst:
                return False
            self._buckets[user_id].append(now)
            return True
        """获取剩余可用请求数"""
        async with self._lock:
            now = time.time()
            if user_id not in self._buckets:
                return self.max_requests + self.burst
            self._buckets[user_id] = [t for t in self._buckets[user_id] if now - t < self.window]
            remaining = self.max_requests + self.burst - len(self._buckets[user_id])
            return max(0, remaining)

    async def reset(self, user_id: str) -> None:
        async with self._lock:
            self._buckets.pop(user_id, None)


class SessionSemaphore:
    """
    全局会话并发控制器

    使用 Semaphore 限制同时运行的 Agent 会话数，
    防止资源耗尽。
    """
    def __init__(self, max_concurrent: int = 100):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_sessions: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, session_id: str, timeout: Optional[float] = None) -> bool:
        """获取会话槽位"""
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            async with self._lock:
                self._active_sessions[session_id] = asyncio.current_task()
            return True
        except asyncio.TimeoutError:
            return False

    async def release(self, session_id: str) -> None:
        """释放会话槽位"""
        async with self._lock:
            self._active_sessions.pop(session_id, None)
        self._semaphore.release()

    async def get_active_count(self) -> int:
        async with self._lock:
            return len(self._active_sessions)

    def get_active_sessions(self) -> List[str]:
        return list(self._active_sessions.keys())

    async def cancel_session(self, session_id: str) -> bool:
        """强制取消指定会话"""
        async with self._lock:
            task = self._active_sessions.get(session_id)
            if task and not task.done():
                task.cancel()
                return True
            return False


class BatchLLMCaller:
    """
    批量 LLM 调用器

    支持：
    - 并发发送多个独立请求
    - 请求去重（相同 prompt 复用结果）
    - 结果缓存
    """
    def __init__(self, max_concurrent: int = 5, cache_ttl: int = 60):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()

    def _make_cache_key(self, messages, tools, temperature, max_tokens) -> str:
        """生成缓存键"""
        import hashlib
        key_data = f"{str(messages)}|{str(tools)}|{temperature}|{max_tokens}"
        return hashlib.md5(key_data.encode()).hexdigest()

    async def call(self, llm, messages, tools=None, temperature=0.7, max_tokens=2048, use_cache=True):
        """单个调用（带缓存和并发控制）"""
        cache_key = self._make_cache_key(messages, tools, temperature, max_tokens)

        if use_cache:
            async with self._lock:
                # 先清理过期缓存，防止无限增长
                now = time.time()
                expired = [k for k, t in self._cache_time.items() if now - t > self._cache_ttl]
                for k in expired:
                    self._cache.pop(k, None)
                    self._cache_time.pop(k, None)

                if cache_key in self._cache:
                    if now - self._cache_time.get(cache_key, 0) < self._cache_ttl:
                        logger.debug(f"LLM cache hit: {cache_key[:8]}")
                        return self._cache[cache_key]

        async with self._semaphore:
            start = time.time()
            try:
                result = await llm.chat(
                    messages=messages, tools=tools,
                    temperature=temperature, max_tokens=max_tokens
                )
                latency = time.time() - start
                logger.debug(f"LLM call completed in {latency:.2f}s")

                if use_cache:
                    async with self._lock:
                        self._cache[cache_key] = result
                        self._cache_time[cache_key] = time.time()

                return result
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                raise

    async def call_batch(self, llm, requests: List[Dict]) -> List[Any]:
        """
        批量并发调用

        Args:
            requests: [{"messages": [...], "tools": [...], "temperature": 0.7, ...}, ...]
        """
        tasks = []
        for req in requests:
            task = self.call(
                llm,
                messages=req["messages"],
                tools=req.get("tools"),
                temperature=req.get("temperature", 0.7),
                max_tokens=req.get("max_tokens", 2048),
                use_cache=req.get("use_cache", True)
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def clear_cache(self) -> None:
        async with self._lock:
            now = time.time()
            expired = [k for k, t in self._cache_time.items() if now - t > self._cache_ttl]
            for k in expired:
                self._cache.pop(k, None)
                self._cache_time.pop(k, None)


class ParallelToolExecutor:
    """
    并行工具执行器

    支持：
    - 同时执行多个无依赖工具
    - 超时控制
    - 错误隔离（单个工具失败不影响其他）
    """
    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def execute_single(self, executor: Callable, action: str, inputs: dict, ctx, timeout: int = 30):
        """执行单个工具"""
        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    executor(action, inputs, ctx),
                    timeout=timeout
                )
                return {"action": action, "success": True, "result": result}
            except asyncio.TimeoutError:
                return {"action": action, "success": False, "error": f"超时 ({timeout}s)"}
            except Exception as e:
                return {"action": action, "success": False, "error": str(e)}

    async def execute_batch(self, executor: Callable, tasks: List[Dict], ctx, timeout: int = 30) -> List[Dict]:
        """
        并行执行多个工具

        Args:
            tasks: [{"action": "tool_name", "inputs": {...}}, ...]
        """
        coros = [
            self.execute_single(executor, t["action"], t["inputs"], ctx, timeout)
            for t in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        # 处理异常
        processed = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                processed.append({
                    "action": tasks[i]["action"],
                    "success": False,
                    "error": str(r)
                })
            else:
                processed.append(r)
        return processed


class AsyncMemoryQueue:
    """
    异步记忆操作队列

    解决 SQLite 等存储的并发写入问题：
    - 所有记忆操作入队，单消费者顺序执行
    - 支持批量合并（减少数据库写入次数）
    - 支持优先级
    """
    def __init__(self, memory_service, batch_size: int = 10, flush_interval: float = 1.0):
        self.memory = memory_service
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._pending: List[Dict] = []
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """启动消费队列"""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Memory queue worker started")

    async def stop(self) -> None:
        """停止消费队列， flush 剩余任务"""
        self._running = False
        if self._worker_task:
            await self._queue.put(None)  # 发送停止信号
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
        # 最后 flush
        await self._flush()
        logger.info("Memory queue worker stopped")

    async def enqueue(self, operation: str, **kwargs) -> None:
        """入队一个记忆操作"""
        await self._queue.put({"op": operation, **kwargs})

    async def _worker(self) -> None:
        """消费者工作协程"""
        last_flush = time.time()
        while self._running:
            try:
                # 等待队列消息或超时
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                if item is None:  # 停止信号
                    break
                async with self._lock:
                    self._pending.append(item)

                # 检查是否需要 flush
                if len(self._pending) >= self.batch_size:
                    await self._flush()
                    last_flush = time.time()
                elif time.time() - last_flush > self.flush_interval:
                    await self._flush()
                    last_flush = time.time()

            except asyncio.TimeoutError:
                # 超时检查是否需要 flush
                if self._pending and time.time() - last_flush > self.flush_interval:
                    await self._flush()
                    last_flush = time.time()
            except asyncio.CancelledError:
                # 允许正常取消，不吞异常
                raise
            except Exception as e:
                logger.error(f"Memory queue worker error: {e}")

    async def _flush(self) -> None:
        """批量执行待处理操作"""
        async with self._lock:
            batch = self._pending[:]
            self._pending = []

        if not batch:
            return

        # 合并相同类型的操作
        stores = [b for b in batch if b["op"] == "store"]
        others = [b for b in batch if b["op"] != "store"]

        # 批量写入
        if stores and self.memory:
            try:
                for s in stores:
                    await self.memory.store(
                        user_id=s.get("user_id", ""),
                        memory_type=s.get("memory_type", "passive"),
                        data=s.get("data", {}),
                        group_id=s.get("group_id"),
                        importance=s.get("importance", 0.5),
                        tags=s.get("tags")
                    )
                logger.debug(f"Memory batch flush: {len(stores)} stores")
            except Exception as e:
                logger.error(f"Memory batch flush failed: {e}")

        # 处理其他操作
        for op in others:
            try:
                if op["op"] == "delete" and self.memory:
                    await self.memory.delete(op.get("memory_id", ""))
                elif op["op"] == "summarize" and self.memory:
                    await self.memory.summarize(op.get("user_id", ""), op.get("level", "24h"))
            except Exception as e:
                logger.error(f"Memory op {op['op']} failed: {e}")


@dataclass
class ConcurrencyConfig:
    """并发配置"""
    max_concurrent_sessions: int = 100
    max_concurrent_llm: int = 5
    max_concurrent_tools: int = 5
    rate_limit_requests: int = 30
    rate_limit_window: int = 60
    rate_limit_burst: int = 5
    memory_queue_batch: int = 10
    memory_queue_flush_interval: float = 1.0
    llm_cache_ttl: int = 60
    enable_parallel_tools: bool = True
    enable_batch_llm: bool = True
    enable_memory_queue: bool = True
