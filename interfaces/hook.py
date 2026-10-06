"""Hook 接口 - 扩展点"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from ..core.state import Priority

class IHook(ABC):
    """钩子接口 - 在关键节点插入自定义逻辑"""

    @property
    @abstractmethod
    def event(self) -> str:
        """
        监听的事件名：
        - on_start: 循环开始
        - before_prepare / after_prepare: 准备阶段
        - before_think / after_think: 思考阶段
        - before_act / after_act: 执行阶段
        - before_observe / after_observe: 观察阶段
        - before_finalize / after_finalize: 结束阶段
        - on_error: 错误处理
        - on_multimodal: 多模态预处理
        """
        pass

    @property
    def priority(self) -> Priority:
        """钩子优先级，数字越小越先执行"""
        return Priority.NORMAL

    @abstractmethod
    async def trigger(self, ctx, **kwargs) -> None:
        """
        触发钩子

        可以通过修改 ctx 来影响后续流程
        """
        pass

    @property
    def name(self) -> str:
        return self.__class__.__name__
