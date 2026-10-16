"""Tool Chain - 工具链模块（阶段二 v2.6.0）

让 LLM 一次声明多步工具计划（DAG 依赖 + 变量传递 + 组内并行），
一次拿回全部观测，替代"每步一次 LLM 往返"的串行模式。

- plan.py: 计划解析与校验（步数上限 / 依赖成环检测 / 禁嵌套）
- chain_executor.py: 拓扑分组执行引擎（同组并行 / abort 传播 / 整链超时兜底）

安全设计：链内禁调 run_tool_chain / delegate_to_subagent（防递归资源耗尽）；
每步经 runner._execute_action 执行，完整继承 auth/self_check/钩子安全链。
"""
from .plan import (
    ChainPlan,
    ChainPlanError,
    ChainStep,
    parse_plan,
    resolve_args,
    topo_groups,
    FORBIDDEN_IN_CHAIN,
)
from .chain_executor import ChainExecutor, ChainReport

__all__ = [
    "ChainPlan",
    "ChainPlanError",
    "ChainStep",
    "parse_plan",
    "resolve_args",
    "topo_groups",
    "ChainExecutor",
    "ChainReport",
    "FORBIDDEN_IN_CHAIN",
]
