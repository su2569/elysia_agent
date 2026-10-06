"""Platform Adapter 接口 - 参考AstrBot"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable, AsyncGenerator
from ..core.context import Message

class IPlatformAdapter(ABC):
    """
    平台适配器接口

    负责连接具体平台（QQ/微信/Discord/Web等）
    将平台消息转换为统一 Message 格式
    """

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def platform_type(self) -> str:
        """平台类型标识"""
        pass

    @abstractmethod
    async def start(self) -> None:
        """启动适配器"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        pass

    @abstractmethod
    async def send_message(
        self,
        target_id: str,
        messages: List[Message],
        **kwargs
    ) -> bool:
        """发送消息到平台"""
        pass

    @abstractmethod
    async def on_message(self, handler: Callable) -> None:
        """注册消息处理器"""
        pass

    @property
    def supports_multimodal(self) -> bool:
        """是否支持多模态"""
        return False

    @property
    def supports_group(self) -> bool:
        """是否支持群聊"""
        return True

    async def health_check(self) -> bool:
        return True
