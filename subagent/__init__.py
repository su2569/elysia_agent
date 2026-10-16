"""SubAgent 模块（阶段二：垂直任务拆分）

提供：
- BaseSubAgent：安全链继承的通用子代理基类
- 内置三垂直代理：ResearchAgent / WriterAgent / AnalystAgent
- register_builtin_subagents()：启动接线入口
"""
from .base import BaseSubAgent
from .builtin import (
    ResearchAgent,
    WriterAgent,
    AnalystAgent,
    register_builtin_subagents,
    unregister_builtin_subagents,
)

__all__ = [
    "BaseSubAgent",
    "ResearchAgent",
    "WriterAgent",
    "AnalystAgent",
    "register_builtin_subagents",
    "unregister_builtin_subagents",
]
