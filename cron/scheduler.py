"""CronScheduler - 定时任务调度器（阶段二收官：cron 模块）

设计原则（继承 ElyBot 防御工程基线）：
1. 安全链继承：定时任务回调统一经 Runner.run() 执行——完整复用
   速率限制/输入消毒/全局超时/PTD 安全引擎，调度器自身不做任何绕过
2. 持久化优先：任务注册即落盘（JSON 原子写），重启自动恢复
3. 超时兜底：单次触发有 timeout 秒硬上限，超时记录并跳过（不阻塞调度循环）
4. 失败隔离：单个任务异常不影响其他任务；连续失败达到阈值自动禁用并告警
5. 防抖防雪崩：错过的时间点只补触发一次（misfire grace），错过超过宽限直接跳到下一周期
6. 最小权限：任务只能由注册方声明回调，回调目标只能是 Runner（不可执行任意代码）
"""
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional
from ..utils.logger import get_logger

logger = get_logger("Cron")

# 允许的调度类型
VALID_KINDS = ("interval", "daily", "weekly")
VALID_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class CronTask:
    """定时任务定义"""
    task_id: str
    name: str                       # 人类可读名
    kind: str                       # interval / daily / weekly
    payload: Dict[str, Any] = field(default_factory=dict)   # Runner.run 参数（query 等）
    # interval 参数
    interval_sec: float = 300.0     # 间隔秒数（interval 型）
    # daily/weekly 参数
    at: str = "08:00"               # HH:MM 触发时刻
    weekdays: List[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])  # weekly 用
    # 运行时控制
    enabled: bool = True
    timeout_sec: float = 120.0      # 单次触发超时
    max_consecutive_failures: int = 3   # 连续失败自动禁用阈值
    # 状态（持久化）
    consecutive_failures: int = 0
    last_run: Optional[float] = None        # epoch
    last_result: Optional[str] = None       # 最近一次结果摘要
    next_run: Optional[float] = None        # 下次调度 epoch
    created_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        """校验任务定义合法性（注册时一次性校验，运行时零校验成本）"""
        if self.kind not in VALID_KINDS:
            raise ValueError(f"未知的调度类型: {self.kind}（可选 {VALID_KINDS}）")
        if not self.name or not self.name.strip():
            raise ValueError("任务名不能为空")
        if not isinstance(self.payload, dict):
            raise ValueError("payload 必须是 dict")
        if self.kind == "interval":
            if not (30 <= self.interval_sec <= 86400 * 7):
                raise ValueError(f"interval_sec 必须在 [30, 604800] 区间（当前 {self.interval_sec}）")
        else:
            hh, mm = self._parse_at(self.at)
            if self.kind == "weekly":
                bad = [w for w in self.weekdays if w not in VALID_WEEKDAYS]
                if not self.weekdays or bad:
                    raise ValueError(f"weekdays 必须是 {sorted(VALID_WEEKDAYS)} 的非空子集（非法值: {bad}）")
        if not (5 <= self.timeout_sec <= 600):
            raise ValueError(f"timeout_sec 必须在 [5, 600] 区间（当前 {self.timeout_sec}）")

    @staticmethod
    def _parse_at(at: str):
        """解析 HH:MM，返回 (h, m)"""
        try:
            parts = at.strip().split(":")
            if len(parts) != 2:
                raise ValueError
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return h, m
        except (ValueError, AttributeError):
            raise ValueError(f"at 必须是 HH:MM 格式（当前 {at!r}）")


def _next_interval(now: float, task: CronTask) -> float:
    """interval 型：下次触发 = 上次触发 + 间隔（首跑 = now + interval）"""
    base = task.last_run if task.last_run else now
    nxt = base + task.interval_sec
    # 防雪崩：若已错过多个周期，只调度到未来的下一个周期点
    if nxt <= now:
        periods = int((now - base) / task.interval_sec) + 1
        nxt = base + periods * task.interval_sec
    return nxt


def _next_calendar(now: float, task: CronTask) -> float:
    """daily/weekly 型：计算下一个日历触发点"""
    import datetime as _dt
    local = _dt.datetime.fromtimestamp(now)
    hh, mm = CronTask._parse_at(task.at)
    today_target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if task.kind == "daily":
        if today_target > local:
            return today_target.timestamp()
        return (today_target + _dt.timedelta(days=1)).timestamp()
    # weekly：找本周内下一个允许的星期
    if today_target > local and WEEKDAY_ORDER[local.weekday()] in task.weekdays:
        return today_target.timestamp()
    for offset in range(1, 8):
        day = local + _dt.timedelta(days=offset)
        if WEEKDAY_ORDER[day.weekday()] in task.weekdays:
            return day.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    # 不可达（weekdays 非空已由 validate 保证）
    raise RuntimeError("weekdays 为空，无法计算下次触发")


class CronScheduler:
    """定时任务调度器

    用法：
        scheduler = CronScheduler(runner_run=runner.run, state_path="data/cron_state.json")
        await scheduler.start()          # 启动调度循环 + 恢复持久化任务
        tid = scheduler.register(...)    # 注册任务（立即持久化）
        await scheduler.stop()           # 停止并持久化
    """

    STATE_VERSION = 1

    def __init__(
        self,
        runner_run: Optional[Callable] = None,
        state_path: str = "./data/cron_state.json",
        poll_interval: float = 30.0,
        misfire_grace_sec: float = 300.0,
    ):
        self._runner_run = runner_run       # async callable: (query=str, ...) -> dict
        self._state_path = state_path
        self._poll_interval = poll_interval
        self._misfire_grace = misfire_grace_sec
        self._tasks: Dict[str, CronTask] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()         # 保护 _tasks 的并发注册/删除

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """启动：恢复持久化任务 + 启动调度循环"""
        if self._running:
            return
        self._load_state()
        # 恢复的任务重算 next_run（进程停摆期间错过的不补触发，直接跳到未来）
        now = time.time()
        for task in self._tasks.values():
            task.next_run = self._compute_next(now, task)
        self._running = True
        self._loop_task = asyncio.get_running_loop().create_task(self._schedule_loop())
        logger.info(f"CronScheduler 启动: {len(self._tasks)} 个任务恢复")

    async def stop(self) -> None:
        """停止：取消调度循环 + 持久化（保留 next_run 供下次重启参考）"""
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError,):
                pass
            self._loop_task = None
        self._save_state()
        logger.info("CronScheduler 已停止")

    # ---------- 任务管理 ----------

    def register(
        self,
        name: str,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        strict: bool = False,
        **kwargs
    ) -> str:
        """注册定时任务（同步 API——注册即持久化）

        Args:
            strict: True 时若落盘失败，撤销内存中的任务并抛 RuntimeError，
                    让调用方（HTTP 接口）能如实回 500；False（默认）保持
                    旧行为，只记日志。默认 False 是为了不打断 Runner 工具接线。

        Returns:
            task_id
        """
        task = CronTask(
            task_id=uuid.uuid4().hex[:12],
            name=name,
            kind=kind,
            payload=payload or {},
            **kwargs
        )
        task.validate()
        self._tasks[task.task_id] = task
        task.next_run = self._compute_next(time.time(), task)
        if not self._save_state():
            if strict:
                self._tasks.pop(task.task_id, None)
                raise RuntimeError("任务状态写入失败，任务未创建")
            logger.error(f"定时任务已注册但未落盘，重启后会丢失: {task.name} ({task.task_id})")
        else:
            logger.info(f"定时任务注册: {task.name} ({task.task_id}) kind={task.kind} next_run={task.next_run}")
        return task.task_id

    async def register_async(self, *args, **kwargs) -> str:
        """异步注册入口（Runner 工具接线用）"""
        return self.register(*args, **kwargs)

    def unregister(self, task_id: str) -> bool:
        """删除任务。

        Returns:
            True 只在「内存已删 且 磁盘已同步」时返回。
            落盘失败时把任务放回内存并返回 False —— 否则接口说删了、
            重启后任务又回来，用户会以为是自己看错了。
        """
        if task_id not in self._tasks:
            return False
        removed = self._tasks.pop(task_id)
        if not self._save_state():
            self._tasks[task_id] = removed
            logger.error(f"定时任务删除未落盘，已回滚: {task_id}")
            return False
        logger.info(f"定时任务删除: {task_id}")
        return True

    def set_enabled(self, task_id: str, enabled: bool) -> bool:
        """启用/禁用任务（持久化语义，重启保持）。

        Returns:
            True 只在「内存已改 且 磁盘已同步」时返回；落盘失败时回滚内存态。
        """
        task = self._tasks.get(task_id)
        if not task:
            return False
        prev_enabled = task.enabled
        prev_failures = task.consecutive_failures
        prev_next = task.next_run
        task.enabled = enabled
        if enabled:
            task.consecutive_failures = 0
            task.next_run = self._compute_next(time.time(), task)
        if not self._save_state():
            task.enabled = prev_enabled
            task.consecutive_failures = prev_failures
            task.next_run = prev_next
            logger.error(f"定时任务启停未落盘，已回滚: {task_id}")
            return False
        return True

    def list_tasks(self) -> List[Dict[str, Any]]:
        """任务清单（WebUI/LLM 可读）"""
        out = []
        for t in self._tasks.values():
            out.append({
                "task_id": t.task_id,
                "name": t.name,
                "kind": t.kind,
                "enabled": t.enabled,
                "interval_sec": t.interval_sec,
                "at": t.at,
                "weekdays": t.weekdays,
                "next_run": t.next_run,
                "last_run": t.last_run,
                "last_result": t.last_result,
                "consecutive_failures": t.consecutive_failures,
            })
        return out

    def get_task(self, task_id: str) -> Optional[CronTask]:
        return self._tasks.get(task_id)

    # ---------- 调度核心 ----------

    async def _schedule_loop(self) -> None:
        """调度主循环：周期性扫描到期任务"""
        while self._running:
            try:
                now = time.time()
                due = [
                    t for t in self._tasks.values()
                    if t.enabled and t.next_run is not None and t.next_run <= now
                ]
                for task in due:
                    # 防抖：错过的触发点若超过宽限，跳过并重算（不补触发）
                    if now - task.next_run > self._misfire_grace:
                        logger.warning(
                            f"任务 {task.name} 错过触发点超过 {self._misfire_grace}s，跳过本次"
                        )
                        task.next_run = self._compute_next(now, task)
                        continue
                    # 创建独立执行任务（互不阻塞）
                    asyncio.get_running_loop().create_task(self._fire(task))
                    task.next_run = self._compute_next(now, task)
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 调度循环自身必须永不死亡
                logger.error(f"调度循环异常（已忽略）: {e}", exc_info=True)
                await asyncio.sleep(self._poll_interval)

    async def _fire(self, task: CronTask) -> None:
        """触发单个任务：经 Runner.run 安全链执行 payload.query"""
        if self._runner_run is None:
            task.last_result = "【错误】未接线 Runner"
            task.consecutive_failures += 1
            self._save_state()
            return
        started = time.time()
        try:
            result = await asyncio.wait_for(
                self._runner_run(**task.payload),
                timeout=task.timeout_sec
            )
            task.last_run = started
            task.last_result = str(result.get("text", ""))[:200] if isinstance(result, dict) else str(result)[:200]
            task.consecutive_failures = 0
            logger.info(
                f"定时任务触发完成: {task.name} ({task.task_id}) 耗时 {time.time()-started:.1f}s"
            )
        except asyncio.TimeoutError:
            task.last_run = started
            task.last_result = f"【超时】超过 {task.timeout_sec}s"
            task.consecutive_failures += 1
            logger.warning(f"定时任务超时: {task.name} ({task.task_id})")
        except Exception as e:
            task.last_run = task.last_run or started
            task.last_result = f"【异常】{str(e)[:150]}"
            task.consecutive_failures += 1
            logger.error(f"定时任务执行异常: {task.name}: {e}", exc_info=True)
        # 连续失败自动禁用（防雪崩 + 提醒人工介入）
        if task.consecutive_failures >= task.max_consecutive_failures:
            task.enabled = False
            logger.warning(
                f"任务 {task.name} 连续失败 {task.consecutive_failures} 次，已自动禁用"
            )
        self._save_state()

    def _compute_next(self, now: float, task: CronTask) -> float:
        """计算下次触发时间"""
        if task.kind == "interval":
            return _next_interval(now, task)
        return _next_calendar(now, task)

    # ---------- 持久化 ----------

    def _save_state(self) -> bool:
        """原子写持久化（临时文件 + rename）。

        Returns:
            True 表示已真正落盘；False 表示写失败（已记 error 日志）。

        历史缺陷（2026-10-21 巡检发现）：本方法原先返回 None，写盘失败只记日志，
        而 register / unregister / set_enabled 的调用方一律回 {"success": True}——
        任务当场在内存里生效，接口说成功，重启后却不见了。这是「报告了成功、
        结果却没发生」的第八处，与 tool_states / user_commands 完全同形。
        """
        state = {
            "version": self.STATE_VERSION,
            "saved_at": time.time(),
            "tasks": [asdict(t) for t in self._tasks.values()],
        }
        tmp = None
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
            return True
        except Exception as e:
            logger.error(f"cron 状态持久化失败: {e}", exc_info=True)
            if tmp:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
            return False

    def _load_state(self) -> None:
        """恢复持久化任务"""
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("version") != self.STATE_VERSION:
                logger.warning("cron 状态文件版本不匹配，忽略")
                return
            for raw in state.get("tasks", []):
                try:
                    task = CronTask(**{
                        k: v for k, v in raw.items()
                        if k in CronTask.__dataclass_fields__
                    })
                    task.validate()
                    self._tasks[task.task_id] = task
                except Exception as e:
                    logger.warning(f"恢复定时任务失败（已跳过）: {e}")
            logger.info(f"cron 状态恢复: {len(self._tasks)} 个任务")
        except Exception as e:
            logger.error(f"cron 状态文件加载失败: {e}", exc_info=True)
            # 损坏原件先留档：任务表是用户手设的提醒，被空表覆盖后无法恢复。
            from ..utils.state_file import backup_corrupt_file
            backup_corrupt_file(self._state_path, logger, reason="定时任务状态解析失败")
