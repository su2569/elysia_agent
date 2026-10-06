"""Elysia Agent - 内置工具集"""
import asyncio
import ast
import json
import operator
import urllib.parse
from datetime import datetime

from ..decorators import tool
from ..utils.logger import get_logger

logger = get_logger("BuiltinTools")

# 安全计算器：AST 节点到运算符的映射
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.FloorDiv: operator.floordiv,
}

def _safe_eval(node):
    """递归解析 AST 节点，仅允许数值运算"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量类型: {type(node.value).__name__}")
    # Python < 3.8 兼容（3.12+ 中 ast.Num 已废弃，但 getattr 仍会触发警告）
    # 通过 __dict__ 检查避免直接访问属性触发 DeprecationWarning
    if 'Num' in ast.__dict__ and isinstance(node, ast.__dict__['Num']):
        return node.n
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPS:
            raise ValueError(f"不支持的运算符: {op_type.__name__}")
        return _SAFE_OPS[op_type](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPS:
            raise ValueError(f"不支持的一元运算符: {op_type.__name__}")
        return _SAFE_OPS[op_type](_safe_eval(node.operand))
    if isinstance(node, ast.Call):
        raise ValueError("禁止函数调用")
    if isinstance(node, ast.Name):
        raise ValueError("禁止变量引用")
    if isinstance(node, ast.Attribute):
        raise ValueError("禁止属性访问")
    if isinstance(node, ast.Subscript):
        raise ValueError("禁止索引访问")
    raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


@tool(name="get_current_time", description="获取当前时间")
async def get_current_time(_ctx=None) -> str:
    now = datetime.now()
    return f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (星期{['一','二','三','四','五','六','日'][now.weekday()]})"


@tool(name="calculator", description="计算器，支持基础数学运算")
async def calculator(expression: str, _ctx=None) -> str:
    """安全计算器 - 只允许基础数学运算"""
    allowed_chars = set("0123456789.+-*/() %")
    if not all(c in allowed_chars for c in expression):
        return "【错误】表达式包含非法字符，仅支持 + - * / % . ( ) 和数字"
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _safe_eval(tree)
        return f"计算结果: {result}"
    except Exception as e:
        return f"【错误】计算失败: {str(e)}"


@tool(name="web_search", description="搜索互联网信息，获取实时资讯")
async def web_search(query: str, _ctx=None) -> str:
    """
    网页搜索 - 使用 DuckDuckGo 即时答案 API（无需API Key）
    如果 DuckDuckGo 不可用，降级到 Bing 或返回提示
    """
    try:
        import aiohttp

        # 尝试 DuckDuckGo HTML 搜索
        encoded_query = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    # 简单提取搜索结果标题和摘要
                    import re
                    results = []
                    # 提取结果链接和标题
                    links = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">([^<]+)</a>', html)
                    snippets = re.findall(r'<a class="result__snippet"[^>]*>([^<]+)</a>', html)

                    for i, (href, title) in enumerate(links[:5]):
                        snippet = snippets[i] if i < len(snippets) else ""
                        # 清理HTML实体
                        title = title.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                        snippet = snippet.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                        results.append(f"{i+1}. {title}\n   {snippet[:120]}...\n   {href}")

                    if results:
                        return f"【搜索结果】关于「{query}」:\n\n" + "\n\n".join(results)
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")

    # 降级：返回结构化提示，建议用户配置搜索API
    return (
        f"【搜索提示】关于「{query}」的实时搜索暂时不可用。\n"
        "建议配置以下搜索API之一以获得最佳体验:\n"
        "1. Serper.dev (Google Search API)\n"
        "2. Bing Search API\n"
        "3. Tavily AI Search API\n"
        "配置方法: 在 .env 中设置 SEARCH_API_KEY 和 SEARCH_PROVIDER"
    )


@tool(name="remember", description="记住一条信息到长期记忆")
async def remember(content: str, category: str = "general", _ctx=None) -> str:
    """存储记忆"""
    if _ctx and hasattr(_ctx, "metadata"):
        weak_mem = _ctx.metadata.get("_memory_weakref")
        memory = weak_mem() if weak_mem else None
        if memory:
            try:
                await memory.store(
                    user_id=_ctx.user_id,
                    memory_type="active",
                    group_id=_ctx.group_id,
                    data={"content": content, "category": category}
                )
                return f"✅ 已记住: [{category}] {content[:50]}..."
            except Exception as e:
                return f"【错误】记忆存储失败: {str(e)}"
    return "【提示】记忆系统未启用"


@tool(name="recall", description="从长期记忆中回忆相关信息")
async def recall(query: str, topk: int = 3, _ctx=None) -> str:
    """检索记忆"""
    if _ctx and hasattr(_ctx, "metadata"):
        weak_mem = _ctx.metadata.get("_memory_weakref")
        memory = weak_mem() if weak_mem else None
        if memory:
            try:
                results = await memory.retrieve(
                    user_id=_ctx.user_id,
                    query=query,
                    include_semantic=topk
                )
                memories = results.get("semantic", [])
                if memories:
                    return "【回忆结果】\n" + "\n".join([f"- {m}" for m in memories[:topk]])
                return "【回忆结果】未找到相关记忆"
            except Exception as e:
                return f"【错误】记忆检索失败: {str(e)}"
    return "【提示】记忆系统未启用"
