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


# =====================================================================
# DeepSeek 官方 Search & Fetch 服务商工具
# 文档: https://api-docs.deepseek.com/guides/search/
#  - Search: POST https://api.deepseek.com/v1/tools/search
#  - Fetch : POST https://api.deepseek.com/v1/tools/fetch
# 鉴权: Authorization: Bearer <DeepSeek API Key>（按请求计费，详见官方文档）
# =====================================================================

_DEEPSEEK_TOOLS_BASE = "https://api.deepseek.com/v1/tools"
_NL = chr(10)  # 换行符（避免源码中转义问题）


def _deepseek_api_key() -> str:
    """解析 DeepSeek 服务商工具所需的 API Key。

    优先级: DEEPSEEK_API_KEY > ELYSIA_LLM_API_KEY > OPENAI_API_KEY
    """
    import os
    for name in ("DEEPSEEK_API_KEY", "ELYSIA_LLM_API_KEY", "OPENAI_API_KEY"):
        key = os.getenv(name, "").strip()
        if key:
            return key
    return ""


def _fmt_deepseek_error(e, endpoint: str) -> str:
    """格式化请求异常信息"""
    return f"【DeepSeek {endpoint} 错误】{type(e).__name__}: {e}"


@tool(
    name="deepseek_search",
    description=(
        "使用 DeepSeek 官方搜索引擎搜索互联网（DeepSeek tools/search），"
        "返回结构化结果（标题/链接/摘要）。适合获取实时资讯与网页检索。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，支持 site: / lang: / after: 等查询操作符，1~128 字符",
            },
            "count": {
                "type": "integer",
                "description": "返回结果数量，1~50，默认 10",
                "minimum": 1,
                "maximum": 50,
            },
            "need_content": {
                "type": "boolean",
                "description": "是否返回每条结果的网页正文 content，默认 false",
            },
        },
        "required": ["query"],
    },
    category="web",
)
async def deepseek_search(query: str, count: int = 10, need_content: bool = False, _ctx=None) -> str:
    """DeepSeek 官方网页搜索（tools/search）"""
    api_key = _deepseek_api_key()
    if not api_key:
        return (
            "【配置缺失】未检测到 DeepSeek API Key。"
            + chr(10)
            + "请在 .env 中设置以下任一变量后重启："
            + chr(10)
            + "  DEEPSEEK_API_KEY  /  ELYSIA_LLM_API_KEY"
        )

    import aiohttp
    url = f"{_DEEPSEEK_TOOLS_BASE}/search"
    payload = {
        "query": query,
        "count": max(1, min(int(count or 10), 50)),
        "need_content": bool(need_content),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"【DeepSeek search 失败】HTTP {resp.status}: {body[:300]}"
                data = await resp.json()
    except Exception as e:
        return _fmt_deepseek_error(e, "search")

    results = data.get("results") or []
    if not results:
        return f"【搜索结果】关于「{query}」未找到相关结果。"

    lines = [f"【DeepSeek 搜索结果】关于「{query}」（共 {len(results)} 条）:{_NL}"]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(无标题)"
        url = r.get("url") or ""
        snippet = r.get("snippet") or r.get("summary") or ""
        site = r.get("site_name") or ""
        date = (r.get("date") or "")[:10]
        meta = " | ".join(x for x in (site, date) if x)
        lines.append(f"{i}. {title}{_NL}   链接: {url}")
        if meta:
            lines.append(f"   来源: {meta}")
        if snippet:
            lines.append(f"   摘要: {snippet[:200]}")
        if need_content and r.get("content"):
            lines.append(f"   正文: {r['content'][:500]}")
    return _NL.join(lines)


@tool(
    name="deepseek_fetch",
    description=(
        "使用 DeepSeek 官方抓取接口（DeepSeek tools/fetch）抓取单个网页，"
        "以 Markdown 文本返回页面正文。适合提取网页详细内容供阅读。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL（http/https，≤8192 字符）",
            },
        },
        "required": ["url"],
    },
    category="web",
)
async def deepseek_fetch(url: str, _ctx=None) -> str:
    """DeepSeek 官方网页抓取（tools/fetch）"""
    api_key = _deepseek_api_key()
    if not api_key:
        return (
            "【配置缺失】未检测到 DeepSeek API Key。"
            + chr(10)
            + "请在 .env 中设置以下任一变量后重启："
            + chr(10)
            + "  DEEPSEEK_API_KEY  /  ELYSIA_LLM_API_KEY"
        )

    if not (url or "").strip().lower().startswith(("http://", "https://")):
        return "【参数错误】url 必须是以 http:// 或 https:// 开头的完整网页地址。"

    import aiohttp
    api_url = f"{_DEEPSEEK_TOOLS_BASE}/fetch"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url, json={"url": url.strip()}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"【DeepSeek fetch 失败】HTTP {resp.status}: {body[:300]}"
                data = await resp.json()
    except Exception as e:
        return _fmt_deepseek_error(e, "fetch")

    title = data.get("title") or "(无标题)"
    content = data.get("content") or ""
    if not content.strip():
        return f"【抓取结果】「{title}」未获取到可用正文（目标站点可能被限制或无可提取内容）。"
    # 限制返回长度，避免上下文超限
    return f"【DeepSeek 抓取结果】{_NL}标题: {title}{_NL}{_NL}{content[:4000]}"
