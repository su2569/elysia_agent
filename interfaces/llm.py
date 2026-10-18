"""LLM Provider 接口"""
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

# 标准工具调用块（OpenAI 兼容格式）
# 各 Provider 将原生协议归一化为此结构：
#   {"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}
ToolCall = Dict[str, Any]


class LLMResponse:
    """LLM响应封装"""
    def __init__(self, content: str, raw: Any = None, usage: Optional[Dict] = None,
                 tool_calls: Optional[List[ToolCall]] = None):
        self.content = content
        self.raw = raw
        self.usage = usage or {}
        # 原生函数调用列表（OpenAI 兼容归一化格式）。None/空 = 模型未发起工具调用
        self.tool_calls = tool_calls or []

    @property
    def has_tool_calls(self) -> bool:
        """是否有原生工具调用"""
        return bool(self.tool_calls)

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
