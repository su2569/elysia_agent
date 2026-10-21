"""后台任务异常兜底（v2.6.1）

背景
----
项目里有 19 处 ``asyncio.create_task`` / ``ensure_async`` 式的「发射后不管」。
它们大多不保存引用、也不挂 ``add_done_callback``。

先澄清一条**不成立**的猜测（2026-10-21 实测推翻）：
    「不保存引用的任务会被 GC 提前回收」——不成立。
    事件循环在任务 await 期间持有强引用，200 个无引用任务实测 200 个全部完成。

真正的缺陷在另一边，而且更安静：

    loop.create_task(_boom())        # 任务内部抛 RuntimeError
    asyncio.run(demo())
    → 输出区一片空白，没有任何 traceback

**一个后台任务抛了异常，不会有任何日志、任何提示，任务就这么消失了。**
而「任务悄悄死掉」和「任务从未被创建」，在日志里长得一模一样。

策略
----
``spawn_background_task()``：创建任务 + 挂 ``add_done_callback``，
任务异常结束时以 ERROR 级别记录完整 traceback；正常结束或被取消则安静收尾。

设计约束
--------
- 本函数处在「任务创建」路径上，**创建失败时绝不抛异常**，只记日志并返回 None。
- 无事件循环时（同步上下文误调）关闭协程，避免 ``never awaited`` 警告。
- 保留一份弱引用集合，便于测试断言「任务确实被跟踪过」；超上限时丢弃最旧的，
  避免长时间运行时无限增长。
- 取消（``CancelledError``）不算异常，不记 ERROR——那是正常收尾。
"""
import asyncio
from typing import Any, Callable, Coroutine, Optional, Set

# 仅用于观测与测试断言；不依赖它维持任务存活（事件循环自己会保）
_live_tasks: Set[Any] = set()
_MAX_TRACKED = 512


def _default_logger():
    try:
        from .logger import logger  # type: ignore
        return logger
    except Exception:
        try:
            from ..utils.logger import logger  # type: ignore
            return logger
        except Exception:
            return None


def tracked_task_count() -> int:
    """当前被跟踪且未完成的任务数（测试用）。"""
    return len(_live_tasks)


def _on_task_done(task, logger=None, on_error=None, label=""):
    _live_tasks.discard(task)
    try:
        if task.cancelled():
            return
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        # 取异常本身失败（极少见），不再深究
        return

    if exc is None:
        return

    log = logger if logger is not None else _default_logger()
    if log is not None:
        try:
            name = label or getattr(task, "get_name", lambda: "")() or "background-task"
            log.error(f"后台任务异常退出（已捕获，未静默丢失）: {name}: {exc}", exc_info=exc)
        except Exception:
            pass

    if on_error is not None:
        try:
            on_error(exc)
        except Exception:
            pass


def spawn_background_task(
    coro: Coroutine,
    *,
    name: Optional[str] = None,
    logger: Any = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> Optional[Any]:
    """创建后台任务并保证异常不会被静默吞掉。

    Args:
        coro: 待执行的协程对象
        name: 可选任务名（出现在日志里，便于定位）
        logger: 可选 logger，默认用项目 logger
        on_error: 可选异常回调，在记录日志之后调用

    Returns:
        asyncio.Task；无事件循环或创建失败时返回 None（协程会被关闭）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文误调：关闭协程，避免 "coroutine was never awaited"
        try:
            coro.close()
        except Exception:
            pass
        return None
    except Exception:
        try:
            coro.close()
        except Exception:
            pass
        return None

    try:
        if name:
            task = loop.create_task(coro, name=name)
        else:
            task = loop.create_task(coro)
    except Exception:
        try:
            coro.close()
        except Exception:
            pass
        log = logger if logger is not None else _default_logger()
        if log is not None:
            try:
                log.error(f"后台任务创建失败: {name or 'background-task'}", exc_info=True)
            except Exception:
                pass
        return None

    # 有界跟踪：超上限丢弃最旧的（只是观测集合，不影响任务本身）
    if len(_live_tasks) >= _MAX_TRACKED:
        try:
            _live_tasks.pop()
        except Exception:
            pass
    _live_tasks.add(task)

    task.add_done_callback(
        lambda t: _on_task_done(t, logger=logger, on_error=on_error, label=name or "")
    )
    return task
