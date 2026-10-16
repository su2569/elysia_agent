"""Builtin SubAgents - 内置垂直子代理（阶段二）

三个垂直角色，各自持有最小权限工具白名单：
- research_agent：联网检索（web_search）+ 时间感知（get_current_time）
- writer_agent：纯创作（无工具，纯 LLM 循环）
- analyst_agent：计算分析（calculator）+ 时间感知
"""
from typing import Set, Dict, Any, Optional
from .base import BaseSubAgent


class ResearchAgent(BaseSubAgent):
    """联网检索子代理：负责信息收集、事实核查、多源交叉验证。"""

    allowed_tools: Set[str] = {"web_search", "get_current_time"}
    system_prompt: str = (
        "你是「检索代理」，负责收集信息与事实核查。\n"
        "工作准则：\n"
        "1. 先用搜索工具收集资料，交叉验证关键事实\n"
        "2. 引用来源时保持客观，区分事实与推测\n"
        "3. 信息足够后立即收尾，输出条理清晰的中文答案\n"
        "4. 搜索不可用时明确说明，不编造来源"
    )


class WriterAgent(BaseSubAgent):
    """创作子代理：负责文案撰写、润色、风格化表达（无工具，纯思考）。"""

    allowed_tools: Set[str] = set()
    system_prompt: str = (
        "你是「创作代理」，负责文案与创作任务。\n"
        "工作准则：\n"
        "1. 严格按任务要求把握文体、语气与篇幅\n"
        "2. 不使用任何工具，直接构思\n"
        "3. 产出为完整可用的成稿，而非大纲或建议"
    )


class AnalystAgent(BaseSubAgent):
    """分析计算子代理：负责数学计算、数据推理、结构化分析。"""

    allowed_tools: Set[str] = {"calculator", "get_current_time"}
    system_prompt: str = (
        "你是「分析代理」，负责计算与逻辑推理。\n"
        "工作准则：\n"
        "1. 涉及数值计算必须调用计算器工具验证，不口算\n"
        "2. 分步推理，每一步的中间结果写入 thought\n"
        "3. 结论必须给出推理链条与最终数值"
    )


_BUILTIN_AGENTS = (ResearchAgent, WriterAgent, AnalystAgent)


def register_builtin_subagents(llm, runner_weakref=None, max_steps: int = 10) -> list:
    """注册全部内置子代理到全局注册中心。

    Args:
        llm: 主 Runner 同款 LLM Provider
        runner_weakref: Runner 的 weakref.ref（安全链继承用）
        max_steps: 默认步数上限

    Returns:
        注册成功的代理实例列表
    """
    from ..registry.subagent_registry import SubAgentRegistry
    registered = []
    for cls in _BUILTIN_AGENTS:
        try:
            instance = cls(llm=llm, runner_ref=runner_weakref, max_steps=max_steps)
            SubAgentRegistry.register(instance)
            registered.append(instance)
        except Exception as e:
            # 注册失败不阻断其他代理
            try:
                from ..utils.logger import get_logger
                get_logger("SubAgent").warning(f"内置子代理 {cls.__name__} 注册失败: {e}")
            except Exception:
                pass
    return registered


def unregister_builtin_subagents() -> None:
    """注销全部内置子代理（测试隔离用）"""
    from ..registry.subagent_registry import SubAgentRegistry
    for cls in _BUILTIN_AGENTS:
        SubAgentRegistry.unregister(cls.__name__)
