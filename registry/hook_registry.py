"""Hook Registry - 钩子注册中心

支持全局默认注册表 + 隔离实例模式。
参考AstrBot的Pipeline设计。
"""
from typing import Dict, List, Any, Optional
import asyncio
from ..interfaces.hook import IHook
from ..core.state import Priority


class HookRegistry:
    """
    钩子注册中心

    支持优先级排序，同一事件可注册多个钩子
    """

    _default_instance: Optional["HookRegistry"] = None

    def __init__(self):
        self._hooks: Dict[str, List[IHook]] = {}
        self._enabled: bool = True

    # ---------- 实例方法 ----------

    def register_instance(self, hook: IHook) -> None:
        """注册钩子到当前实例"""
        event = hook.event
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(hook)
        self._hooks[event].sort(key=lambda h: h.priority.value)

    def unregister_instance(self, hook_name: str, event: Optional[str] = None) -> bool:
        """从当前实例注销钩子"""
        if event:
            if event in self._hooks:
                self._hooks[event] = [h for h in self._hooks[event] if h.name != hook_name]
                return True
            return False
        else:
            for ev, hooks in self._hooks.items():
                self._hooks[ev] = [h for h in hooks if h.name != hook_name]
            return True

    async def trigger_instance(self, event: str, ctx, **kwargs) -> None:
        """触发当前实例的事件"""
        if not self._enabled or event not in self._hooks:
            return
        for hook in self._hooks.get(event, []):
            try:
                await hook.trigger(ctx, **kwargs)
            except asyncio.CancelledError:
                raise  # 不吞取消异常，允许上层正确终止
            except Exception as e:
                from ..utils.logger import get_logger
                get_logger("HookRegistry").error(f"Hook {hook.name} error: {e}")

    def list_events_instance(self) -> List[str]:
        return list(self._hooks.keys())

    def list_hooks_instance(self, event: Optional[str] = None) -> List[str]:
        if event:
            return [h.name for h in self._hooks.get(event, [])]
        result = []
        for hooks in self._hooks.values():
            result.extend([h.name for h in hooks])
        return result

    def clear_instance(self) -> None:
        self._hooks.clear()

    def enable_instance(self) -> None:
        self._enabled = True

    def disable_instance(self) -> None:
        self._enabled = False

    # ---------- 类方法（操作全局默认实例） ----------

    @classmethod
    def _get_default(cls) -> "HookRegistry":
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    @classmethod
    def register(cls, hook: IHook) -> None:
        cls._get_default().register_instance(hook)

    @classmethod
    def unregister(cls, hook_name: str, event: Optional[str] = None) -> bool:
        return cls._get_default().unregister_instance(hook_name, event)

    @classmethod
    async def trigger(cls, event: str, ctx, **kwargs) -> None:
        await cls._get_default().trigger_instance(event, ctx, **kwargs)

    @classmethod
    def list_events(cls) -> List[str]:
        return cls._get_default().list_events_instance()

    @classmethod
    def list_hooks(cls, event: Optional[str] = None) -> List[str]:
        return cls._get_default().list_hooks_instance(event)

    @classmethod
    def clear(cls) -> None:
        cls._get_default().clear_instance()

    @classmethod
    def enable(cls) -> None:
        cls._get_default().enable_instance()

    @classmethod
    def disable(cls) -> None:
        cls._get_default().disable_instance()

    @classmethod
    def create_isolated(cls) -> "HookRegistry":
        """创建隔离实例（用于测试）"""
        return cls()
