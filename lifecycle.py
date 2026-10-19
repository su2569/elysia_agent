"""Elysia Agent - 进程保活 / 常驻服务（v2.5.0）

设计目标（模仿 AstrBot / MaiBot 的常驻运行方式）：
- Bot 服务作为常驻进程运行，任何情况下都不应因单个模块/平台异常而结束 Python 进程
- 平台崩溃自动重启（指数退避），Runner 异常自动降级
- 优雅关闭：SIGINT / SIGTERM / Ctrl+C 时按顺序清理资源
- 全局异常兜底：asyncio 事件循环异常只记录日志，不退出进程

用法:
    from elysia_agent.lifecycle import ServiceHost

    host = ServiceHost(runner=runner, platforms=[...])
    await host.run_forever()   # 阻塞，直到收到关闭信号

    # 或作为异步上下文管理器（配合 uvicorn 等服务）:
    async with ServiceHost(runner=runner, platforms=[...]):
        await some_server_serve()
"""
import asyncio
import signal
import time
from typing import Dict, Any, List, Optional

from .utils.logger import get_logger

logger = get_logger("Lifecycle")

# 监督间隔下界与默认值。
# 传 0 或负数时 asyncio.wait_for(timeout=0) 会立即返回，形成忙循环
# （实测 interval=0 时 0.3 秒内扫描 17580 次，吃满一个核心）。
MIN_SUPERVISOR_INTERVAL = 0.5
DEFAULT_SUPERVISOR_INTERVAL = 5.0


def _coerce_interval(value, default: float = DEFAULT_SUPERVISOR_INTERVAL) -> float:
    """把任意输入收敛成合法的监督间隔。

    非法值（None / 字符串 / nan / inf）一律回落默认值；
    数值则夹取到 [MIN_SUPERVISOR_INTERVAL, +inf)。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):  # nan / inf
        return default
    if v < MIN_SUPERVISOR_INTERVAL:
        return MIN_SUPERVISOR_INTERVAL
    return v


def _platform_name(platform) -> str:
    """安全地取平台名：name -> platform_type -> unknown，全程不抛。"""
    for attr in ("name", "platform_type"):
        try:
            v = getattr(platform, attr, None)
        except Exception:
            v = None
        if v:
            return str(v)
    return "unknown"


class PlatformSupervisor:
    """单平台监督器：平台崩溃/断线时自动重启，不结束宿主进程

    策略：
    - 平台未 running 且未在启动中 -> 尝试启动
    - 启动失败 -> 指数退避（1s, 2s, 4s, ... 上限 60s）
    - 连续失败超过 max_failures -> 标记为 disabled（仍不退出进程）
    - 可通过 `restart(name)` 手动恢复
    """

    def __init__(self, platform, max_failures: int = 10, max_backoff: float = 60.0):
        self.platform = platform
        self.max_failures = max_failures
        self.max_backoff = max_backoff
        self.failures = 0
        self.restarts = 0
        self.disabled = False
        self.last_error: Optional[str] = None
        self._backoff = 1.0
        # 退避截止时刻（time.monotonic）。到点前 ensure_running 直接返回 False，
        # 不再在内部 sleep——否则一个平台的退避会串行拖住整条监督链。
        self._next_retry_at = 0.0

    @property
    def name(self) -> str:
        return getattr(self.platform, "name", getattr(self.platform, "platform_type", "unknown"))

    def _next_backoff(self) -> float:
        self._backoff = min(self._backoff * 2, self.max_backoff)
        return self._backoff

    def reset_backoff(self) -> None:
        self._backoff = 1.0
        self._next_retry_at = 0.0

    async def ensure_running(self) -> bool:
        """确保平台正在运行。返回 True 表示平台可用。"""
        if self.disabled:
            return False
        try:
            running = bool(getattr(self.platform, "running", False))
        except Exception:
            running = False
        if running:
            self.reset_backoff()
            return True

        # 仍在退避窗口内：直接放弃本次，把控制权让给其它平台
        if time.monotonic() < self._next_retry_at:
            return False

        try:
            await self.platform.start()
            self.failures = 0
            self.restarts += 1
            self.reset_backoff()
            self.last_error = None
            logger.info(f"平台 {self.name} 已启动（累计重启 {self.restarts} 次）")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.failures += 1
            self.last_error = str(e)
            if self.failures >= self.max_failures:
                self.disabled = True
                logger.error(f"平台 {self.name} 连续失败 {self.failures} 次，已禁用（进程继续运行）")
            else:
                backoff = self._next_backoff()
                self._next_retry_at = time.monotonic() + backoff
                logger.warning(f"平台 {self.name} 启动失败（{self.failures}/{self.max_failures}）: {e}，"
                               f"{backoff:.1f}s 后重试")
            return False

    async def restart(self) -> bool:
        """手动重启平台（恢复被禁用的平台）"""
        self.disabled = False
        self.failures = 0
        try:
            await self.platform.stop()
        except Exception as e:
            logger.warning(f"平台 {self.name} 停止失败: {e}")
        return await self.ensure_running()


class ServiceHost:
    """
    常驻服务管理器（进程保活）

    保证：
    1. 单个平台崩溃/断线 -> 自动重启，不影响其他平台，不结束进程
    2. 全局 asyncio 异常 -> 记录日志，进程继续运行
    3. SIGINT / SIGTERM / Ctrl+C -> 优雅关闭（停止平台 -> 停止 Runner）
    4. run_forever() 阻塞运行，适合作为 Bot 常驻进程

    用法:
        host = ServiceHost(runner=runner, platforms=[platforms])
        await host.run_forever()
    """

    def __init__(
        self,
        runner=None,
        platforms: Optional[List] = None,
        supervisor_interval: float = 5.0,
        max_failures: int = 10,
        max_backoff: float = 60.0,
        auto_supervise: bool = True,
    ):
        self.runner = runner
        self.platforms = list(platforms or [])
        self.supervisor_interval = _coerce_interval(supervisor_interval)
        self.auto_supervise = auto_supervise
        self._supervisors: Dict[str, PlatformSupervisor] = {}
        # id(platform) -> supervisor。名字可能重复，按对象查才不会串。
        self._by_platform: Dict[int, PlatformSupervisor] = {}
        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._closed = False
        self._started = False

        for p in self.platforms:
            base = _platform_name(p)
            key = base
            # 同名平台若直接以名字为键会互相覆盖：前一个既不启动也不监督，
            # 且没有任何日志。这里加后缀区分，两个都留在表里。
            n = 2
            while key in self._supervisors:
                key = f"{base}#{n}"
                n += 1
            if key != base:
                logger.warning(f"平台名重复：{base}，本次以 {key} 登记（两个都会被监督）")
            sup = PlatformSupervisor(p, max_failures=max_failures, max_backoff=max_backoff)
            self._supervisors[key] = sup
            self._by_platform[id(p)] = sup

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动 Runner 与所有平台（不阻塞）"""
        if self._started:
            return
        self._started = True

        if self.runner is not None:
            try:
                await self.runner.start()
                logger.info("Runner 已启动")
            except Exception as e:
                logger.error(f"Runner 启动失败（进程继续运行）: {e}")

        if self.auto_supervise:
            for p in self.platforms:
                sup = self.supervisor_of(p)
                try:
                    await sup.ensure_running()
                except Exception as e:
                    logger.warning(f"平台 {sup.name} 初始启动失败: {e}")

        self._install_loop_exception_handler()

        if self.auto_supervise and self.platforms:
            self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="platform-supervisor"))

    async def stop(self) -> None:
        """优雅停止：停止平台 -> 停止 Runner"""
        if self._closed:
            return
        self._closed = True

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        for p in self.platforms:
            # 注意：getattr 的第三个参数会先求值，写成
            # getattr(p, "name", p.platform_type) 时即使 name 存在也会访问
            # platform_type，平台缺该属性就抛 AttributeError，导致后面的平台
            # 和 Runner 全都停不下来。这里嵌套兜底。
            label = _platform_name(p)
            try:
                await p.stop()
                logger.info(f"平台 {label} 已停止")
            except Exception as e:
                logger.warning(f"平台 {label} 停止异常: {e}")

        if self.runner is not None:
            try:
                await self.runner.stop()
                logger.info("Runner 已停止")
            except Exception as e:
                logger.warning(f"Runner 停止异常: {e}")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    # ==================== 常驻运行 ====================

    async def run_forever(self) -> None:
        """
        常驻运行，直到收到关闭信号（SIGINT / SIGTERM / Ctrl+C）或调用 shutdown()。

        不结束 Python 进程：任何异常都被兜底处理，仅记录日志。
        """
        await self.start()

        loop = asyncio.get_running_loop()
        installed = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.shutdown)
                installed = True
            except (NotImplementedError, RuntimeError):
                pass  # Windows 下 add_signal_handler 不可用，用 KeyboardInterrupt 兜底

        logger.info(f"Elysia Agent 常驻服务运行中（平台: {len(self.platforms)} 个），Ctrl+C 优雅停止...")
        try:
            await self._stop_event.wait()
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在优雅停止...")
        finally:
            if installed:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.remove_signal_handler(sig)
                    except Exception:
                        pass
            await self.stop()

    def shutdown(self) -> None:
        """请求关闭（可安全地从信号处理器中调用）"""
        if not self._stop_event.is_set():
            logger.info("收到关闭信号，正在停止常驻服务...")
            self._stop_event.set()

    # ==================== 监督 ====================

    async def _supervisor_loop(self) -> None:
        """平台监督循环：周期检查所有平台，崩溃自动重启（不退出进程）"""
        while not self._stop_event.is_set():
            for name, sup in self._supervisors.items():
                if self._stop_event.is_set():
                    break
                try:
                    await sup.ensure_running()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"监督平台 {name} 异常: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.supervisor_interval)
            except asyncio.TimeoutError:
                pass

    def supervisor_of(self, platform) -> Optional[PlatformSupervisor]:
        """按平台对象取监督器（同名平台下比按名字取更可靠）"""
        sup = self._by_platform.get(id(platform))
        if sup is not None:
            return sup
        for s in self._supervisors.values():
            if s.platform is platform:
                return s
        return None

    async def restart_platform(self, name: str) -> bool:
        """手动重启指定平台"""
        sup = self._supervisors.get(name)
        if sup is None:
            logger.warning(f"未找到平台: {name}")
            return False
        return await sup.restart()

    def platform_status(self) -> List[Dict[str, Any]]:
        """平台状态快照（供 CLI/WebUI 展示）"""
        status = []
        for name, sup in self._supervisors.items():
            try:
                running = bool(getattr(sup.platform, "running", False))
            except Exception:
                running = False
            status.append({
                "name": name,
                "type": getattr(sup.platform, "platform_type", "unknown"),
                "running": running,
                "restarts": sup.restarts,
                "failures": sup.failures,
                "disabled": sup.disabled,
                "last_error": sup.last_error,
            })
        return status

    # ==================== 全局异常兜底 ====================

    def _install_loop_exception_handler(self) -> None:
        """安装 asyncio 全局异常处理器：记录日志，不退出进程"""
        loop = asyncio.get_running_loop()

        def _handler(loop_, context: Dict[str, Any]):
            exc = context.get("exception")
            message = context.get("message", "未知名异常")
            if isinstance(exc, asyncio.CancelledError):
                return  # 取消不算异常
            logger.error(f"事件循环异常（进程继续运行）: {message}"
                         + (f" | {type(exc).__name__}: {exc}" if exc else ""))
            # 打印任务栈，便于定位
            fut = context.get("future")
            if fut is not None:
                try:
                    fut.print_stack(limit=3)
                except Exception:
                    pass

        loop.set_exception_handler(_handler)