"""Tool Chain - 链执行引擎

按拓扑组顺序执行计划：
- 同组步骤并行执行（受 ParallelToolExecutor 信号量约束，无则裸 gather）
- 每步经 runner._execute_action 执行 -> 完整继承 auth/self_check/钩子安全链
- abort 语义：失败的 abort 步骤，其全部下游（含间接依赖）标记跳过
- 整链外层超时兜底，防 LLM 生成的复杂链拖垮会话
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable

from ..utils.logger import get_logger
from .plan import ChainPlan, ChainStep, topo_groups, resolve_args

logger = get_logger("ToolChain")


@dataclass
class ChainReport:
    """链执行报告（返回给 LLM 的观测）"""
    success: bool = False
    total: int = 0
    executed: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def render(self) -> str:
        """渲染为 LLM 可读的观测文本"""
        status = "成功" if self.failed == 0 and self.skipped == 0 else (
            "部分失败" if self.executed > 0 else "失败"
        )
        lines = [
            f"【工具链 {status}】共 {self.total} 步，"
            f"执行 {self.executed}（失败 {self.failed}，跳过 {self.skipped}），"
            f"耗时 {self.duration:.2f}s"
        ]
        for sid, entry in self.results.items():
            state = entry.get("state", "done")
            output = str(entry.get("result", ""))
            if len(output) > 800:
                output = output[:800] + "...(截断)"
            mark = {"done": "✔", "failed": "✘", "skipped": "⊘"}.get(state, "·")
            lines.append(f"{mark} {sid} [{entry.get('tool', '?')}] {state}: {output}")
        return "\n".join(lines)


class ChainExecutor:
    """工具链执行器"""

    def __init__(
        self,
        execute_action: Callable,
        max_concurrent: int = 5,
        result_cap: int = 20000,
    ):
        """
        Args:
            execute_action: runner._execute_action（async (action, inputs, ctx) -> str）
            max_concurrent: 组内并行上限
            result_cap: 结果总字节上限（防超长观测挤爆上下文）
        """
        self._execute_action = execute_action
        self._semaphore: Optional[asyncio.Semaphore] = None
        self.max_concurrent = max(1, int(max_concurrent))
        self.result_cap = result_cap

    async def run(
        self,
        plan: ChainPlan,
        ctx,
        default_timeout: float = 30.0,
        overall_timeout: Optional[float] = None,
    ) -> ChainReport:
        """执行链计划

        Args:
            plan: 已校验的链计划
            ctx: AgentContext
            default_timeout: 单步默认超时（settings.tool.timeout_per_tool）
            overall_timeout: 整链超时（默认 = 步数 * 单步超时，上限 180s）
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)

        if overall_timeout is None:
            overall_timeout = min(plan.steps.__len__() * default_timeout, 180.0)

        started = time.monotonic()
        report = ChainReport(total=len(plan.steps))
        # 依赖图预计算：供 abort 传播
        dependents_map: Dict[str, List[str]] = {}
        for s in plan.steps:
            for dep in s.depends_on:
                dependents_map.setdefault(dep, []).append(s.id)

        try:
            await asyncio.wait_for(
                self._run_groups(plan, ctx, report, default_timeout, dependents_map),
                timeout=overall_timeout,
            )
            report.success = report.failed == 0 and report.skipped == 0
        except asyncio.TimeoutError:
            logger.warning(f"工具链整链超时（{overall_timeout:.0f}s），已执行 {report.executed} 步")
            # 未完成的步骤标记为超时跳过
            for step in plan.steps:
                if step.id not in report.results:
                    report.results[step.id] = {
                        "tool": step.tool, "state": "skipped",
                        "result": "整链超时被中止",
                    }
                    report.skipped += 1
        except Exception as e:
            logger.error(f"工具链执行异常: {e}", exc_info=True)
            for step in plan.steps:
                if step.id not in report.results:
                    report.results[step.id] = {
                        "tool": step.tool, "state": "skipped",
                        "result": f"链异常中止: {e}",
                    }
                    report.skipped += 1

        report.duration = time.monotonic() - started
        self._cap_results(report)
        return report

    async def _run_groups(
        self,
        plan: ChainPlan,
        ctx,
        report: ChainReport,
        default_timeout: float,
        dependents_map: Dict[str, List[str]],
    ) -> None:
        groups = topo_groups(plan)
        skip_ids: set = set()

        for group in groups:
            runnable = [s for s in group if s.id not in skip_ids]
            for s in group:
                if s.id in skip_ids:
                    report.results[s.id] = {
                        "tool": s.tool, "state": "skipped",
                        "result": "上游失败被跳过（on_fail=abort）",
                    }
                    report.skipped += 1

            if not runnable:
                continue

            tasks = [
                asyncio.create_task(self._run_step(s, ctx, report, default_timeout))
                for s in runnable
            ]
            outcomes = await asyncio.gather(*tasks)

            # abort 传播：失败步骤的全部下游标记跳过
            for step, ok in zip(runnable, outcomes):
                if not ok:
                    if step.on_fail == "abort":
                        self._mark_descendants(step.id, dependents_map, skip_ids)

    def _mark_descendants(self, root: str, dependents_map: Dict[str, List[str]], skip_ids: set) -> None:
        """BFS 收集 root 的全部下游步骤 id"""
        queue = list(dependents_map.get(root, []))
        while queue:
            sid = queue.pop(0)
            if sid in skip_ids:
                continue
            skip_ids.add(sid)
            queue.extend(dependents_map.get(sid, []))

    async def _run_step(self, step: ChainStep, ctx, report: ChainReport, default_timeout: float) -> bool:
        """执行单步；返回是否成功"""
        timeout = step.timeout or default_timeout
        resolved = resolve_args(step.args, report.results)
        try:
            async with self._semaphore:
                raw = await asyncio.wait_for(
                    self._execute_action(step.tool, resolved, ctx),
                    timeout=timeout,
                )
            report.results[step.id] = {"tool": step.tool, "state": "done", "result": raw}
            report.executed += 1
            return True
        except asyncio.TimeoutError:
            err = f"执行超时（{timeout:.0f}s）"
        except Exception as e:
            err = f"执行异常: {e}"
            logger.warning(f"链步骤 {step.id}({step.tool}) 异常: {e}")

        report.results[step.id] = {"tool": step.tool, "state": "failed", "result": err}
        report.executed += 1
        report.failed += 1
        return False

    def _cap_results(self, report: ChainReport) -> None:
        """结果体积控制：渲染文本超限时压缩最长的结果"""
        for _ in range(8):
            text = report.render()
            if len(text) <= self.result_cap:
                return
            # 找最长的结果砍半
            longest = max(report.results.items(), key=lambda kv: len(str(kv[1].get("result", ""))))
            sid, entry = longest
            cur = str(entry.get("result", ""))
            entry["result"] = cur[: max(50, len(cur) // 2)] + "...(压缩)"
        # 极端情况仍超限：清空最长者
        if len(report.render()) > self.result_cap * 2:
            for entry in report.results.values():
                entry["result"] = str(entry.get("result", ""))[:100] + "...(截断)"
