"""Tool Chain - 工具链计划解析与校验

LLM 可通过一次 run_tool_chain 调用声明多步工具执行计划：
- 顺序依赖：步骤间以 $id / $id.result 引用前序输出
- 组内并行：无依赖关系的步骤自动归入同一执行组并发执行
- 安全约束：步数上限、依赖成环检测、禁止嵌套链/子代理（防资源耗尽）
"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import re

# 链内禁止出现的动作（防套娃链 / 递归资源耗尽）
FORBIDDEN_IN_CHAIN = {"run_tool_chain", "delegate_to_subagent"}

MAX_ID_LEN = 32
MAX_STEPS_HARD_LIMIT = 32


class ChainPlanError(ValueError):
    """计划非法（LLM 生成的计划不符合约束）"""


@dataclass
class ChainStep:
    """单个链步骤"""
    id: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    on_fail: str = "continue"       # abort: 后续依赖步骤跳过 | continue: 错误文本继续传递
    timeout: Optional[float] = None  # None -> 沿用全局 timeout_per_tool


@dataclass
class ChainPlan:
    """工具链计划"""
    steps: List[ChainStep] = field(default_factory=list)
    description: str = ""

    @property
    def step_ids(self) -> set:
        return {s.id for s in self.steps}


def parse_plan(data: Dict[str, Any], max_steps: int = 8) -> ChainPlan:
    """解析并校验 LLM 生成的计划

    输入结构:
    {
      "description": "可选说明",
      "steps": [
        {"id": "s1", "tool": "web_search", "args": {"query": "..."}, "depends_on": [], "on_fail": "continue"},
        {"id": "s2", "tool": "calculator", "args": {"expression": "$s1.result * 2"}, "depends_on": ["s1"]}
      ]
    }
    """
    if not isinstance(data, dict):
        raise ChainPlanError("计划必须是 JSON 对象")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ChainPlanError("steps 必须是非空数组")

    limit = min(max(int(max_steps), 1), MAX_STEPS_HARD_LIMIT)
    if len(raw_steps) > limit:
        raise ChainPlanError(f"步骤数 {len(raw_steps)} 超过上限 {limit}")

    plan = ChainPlan(description=str(data.get("description", ""))[:200])
    seen_ids: set = set()

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ChainPlanError(f"步骤[{i}] 必须是对象")

        sid = str(raw.get("id", "")).strip()
        if not sid or len(sid) > MAX_ID_LEN:
            raise ChainPlanError(f"步骤[{i}] id 非法（需 1~{MAX_ID_LEN} 字符）")
        if sid in seen_ids:
            raise ChainPlanError(f"步骤 id 重复: {sid}")
        seen_ids.add(sid)

        tool = str(raw.get("tool", "")).strip()
        if not tool:
            raise ChainPlanError(f"步骤 {sid} 缺少 tool")
        if tool in FORBIDDEN_IN_CHAIN:
            raise ChainPlanError(f"步骤 {sid} 不允许嵌套调用 {tool}")

        args = raw.get("args", {})
        if not isinstance(args, dict):
            raise ChainPlanError(f"步骤 {sid} 的 args 必须是对象")

        depends_on = raw.get("depends_on", [])
        if depends_on is None:
            depends_on = []
        if not isinstance(depends_on, list):
            raise ChainPlanError(f"步骤 {sid} 的 depends_on 必须是数组")
        depends_on = [str(d) for d in depends_on]

        on_fail = str(raw.get("on_fail", "continue")).strip().lower()
        if on_fail not in ("abort", "continue"):
            on_fail = "continue"

        timeout = raw.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
                if timeout <= 0 or timeout > 300:
                    timeout = None
            except (TypeError, ValueError):
                timeout = None

        plan.steps.append(ChainStep(
            id=sid, tool=tool, args=dict(args),
            depends_on=depends_on, on_fail=on_fail, timeout=timeout,
        ))

    # 依赖引用合法性：只能引用已声明的步骤 id
    for step in plan.steps:
        for dep in step.depends_on:
            if dep not in seen_ids:
                raise ChainPlanError(f"步骤 {step.id} 依赖了未定义的步骤: {dep}")

    topo_groups(plan)  # 成环检测（失败抛 ChainPlanError）
    return plan


def topo_groups(plan: ChainPlan) -> List[List[ChainStep]]:
    """拓扑分组：无依赖关系的步骤归入同组（组内可并行）

    Kahn 算法变体；成环抛 ChainPlanError
    """
    by_id = {s.id: s for s in plan.steps}
    indegree = {s.id: 0 for s in plan.steps}
    dependents: Dict[str, List[str]] = {s.id: [] for s in plan.steps}

    for s in plan.steps:
        for dep in set(s.depends_on):  # 去重防重复计数
            indegree[s.id] += 1
            dependents[dep].append(s.id)

    groups: List[List[ChainStep]] = []
    ready = [sid for sid, d in indegree.items() if d == 0]
    processed = 0

    while ready:
        groups.append([by_id[sid] for sid in ready])
        next_ready: List[str] = []
        for sid in ready:
            for child in dependents[sid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
            processed += 1
        ready = next_ready

    if processed != len(plan.steps):
        unresolved = [sid for sid, d in indegree.items() if d > 0]
        raise ChainPlanError(f"依赖关系成环，无法执行: {unresolved}")
    return groups


def resolve_args(args: Dict[str, Any], results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """变量替换：将 args 中的 $id / $id.result 占位符替换为前序步骤结果

    - $id.result -> 该步骤结果文本（截断防提示词爆炸）
    - $id        -> 同 $id.result 的简写
    - 失败步骤的结果替换为错误说明文本（on_fail=continue 语义）
    """
    import re
    resolved: Dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            resolved[key] = _substitute(value, results)
        elif isinstance(value, list):
            resolved[key] = [
                _substitute(v, results) if isinstance(v, str) else v for v in value
            ]
        else:
            resolved[key] = value
    return resolved


_VAR_PATTERN = re.compile(r"\$([A-Za-z0-9_]+)(?:\.result)?")


def _substitute(text: str, results: Dict[str, Dict[str, Any]]) -> str:
    def _repl(m: "re.Match") -> str:
        sid = m.group(1)
        entry = results.get(sid)
        if entry is None:
            return m.group(0)  # 未知引用原样保留（解析层已拦截，此处兜底）
        output = str(entry.get("result", ""))
        if len(output) > 2000:  # 截断防提示词爆炸
            output = output[:2000] + "...(截断)"
        return output

    return _VAR_PATTERN.sub(_repl, text)
