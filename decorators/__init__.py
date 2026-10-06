"""Elysia Agent - 装饰器

提供 @tool, @subagent, @hook 装饰器，简化插件开发
"""
from typing import Callable, Optional, Dict, Any
import asyncio
from ..interfaces.tool import ITool
from ..interfaces.subagent import ISubAgent
from ..interfaces.hook import IHook
from ..registry.tool_registry import ToolRegistry
from ..registry.subagent_registry import SubAgentRegistry
from ..registry.hook_registry import HookRegistry
from ..core.state import Priority

def tool(name: Optional[str] = None, description: str = "", parameters: Optional[Dict] = None, category: str = "general"):
    """工具装饰器"""
    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__
        tool_desc = description or func.__doc__ or ""
        tool_params = parameters or {"type": "object", "properties": {}}

        class DynamicTool(ITool):
            @property
            def name(self): return tool_name
            @property
            def description(self): return tool_desc
            @property
            def parameters(self): return tool_params
            @property
            def category(self): return category
            async def execute(self, **kwargs):
                # 过滤掉内部参数，但保留 _ctx
                clean_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_") or k == "_ctx"}
                if asyncio.iscoroutinefunction(func):
                    return await func(**clean_kwargs)
                return func(**clean_kwargs)

        ToolRegistry.register(DynamicTool())
        return func
    return decorator

def subagent(name: Optional[str] = None, description: str = ""):
    """子代理装饰器（类装饰器）"""
    def decorator(cls):
        agent_name = name or cls.__name__
        agent_desc = description or cls.__doc__ or ""

        original_run = cls.run

        class DynamicSubAgent(ISubAgent):
            @property
            def name(self): return agent_name
            @property
            def description(self): return agent_desc
            async def run(self, task: str, context=None, parent_ctx=None):
                instance = cls()
                return await original_run(instance, task, context, parent_ctx)

        SubAgentRegistry.register(DynamicSubAgent())
        return cls
    return decorator

def hook(event: str, priority: Priority = Priority.NORMAL):
    """钩子装饰器"""
    def decorator(func: Callable) -> Callable:
        class DynamicHook(IHook):
            @property
            def event(self): return event
            @property
            def priority(self): return priority
            @property
            def name(self): return func.__name__
            async def trigger(self, ctx, **kwargs):
                if asyncio.iscoroutinefunction(func):
                    await func(ctx, **kwargs)
                else:
                    func(ctx, **kwargs)

        HookRegistry.register(DynamicHook())
        return func
    return decorator
