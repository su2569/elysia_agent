"""LLM Provider 接口"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class LLMResponse:
    """LLM响应封装"""
    def __init__(self, content: str, raw: Any = None, usage: Optional[Dict] = None):
        self.content = content
        self.raw = raw
        self.usage = usage or {}

    def __str__(self):
        return self.content

class ILLMProvider(ABC):
    """大模型接口适配器"""

    @property
    @abstractmethod
    def name(self) -> str: pass

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        **kwargs
    ) -> LLMResponse:
        """
        发起对话请求

        Args:
            messages: OpenAI格式的消息列表
            tools: 工具定义列表
            temperature: 温度
            max_tokens: 最大token数
        """
        pass

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """获取文本嵌入向量"""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        pass
