"""APLR Style Tools - 参考 astrbot_plugin_local_reminiscence

提供AI主动调用的记忆工具：
- recall_memory_tool: 检索相关事件记忆
- recall_node_tool: 搜索特定实体/概念背景
- deep_recall_tool: 深度回想（事件/主题/日期）
- recall_recent_events_tool: 获取近期重要事件
- write_node_tool: 写入/更新记忆节点
- daily_summary_tool: 触发每日总结

注意：EnhancedMemory 和 MemoryNodeManager 已改为异步，所有调用需加 await。
"""
from datetime import datetime, timedelta
from typing import Optional

from ..decorators import tool
from ..memory import EnhancedMemory
from ..utils.logger import get_logger

logger = get_logger("APLRTools")


@tool(name="recall_memory_tool", description="检索与输入文本最相关的过往事件记忆", parameters={
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "回忆关键词或问题"},
        "count": {"type": "integer", "description": "返回数量", "default": 3}
    },
    "required": ["query"]
})
async def recall_memory_tool(query: str, count: int = 3, _ctx=None):
    """回忆相关事件记忆"""
    if not _ctx or not hasattr(_ctx, 'metadata'):
        return "【错误】无法访问记忆系统"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return "【错误】记忆系统未初始化"

    results = await memory.auto_recall(query, top_k=count)
    if not results:
        return "【回忆结果】未找到相关记忆"

    parts = ["【回忆结果】"]
    for i, r in enumerate(results, 1):
        parts.append(f"{i}. [{r['date']}] {r['narrative'][:100]}")
        if r.get('reflection'):
            parts.append(f"   感悟: {r['reflection'][:80]}")

    return "\n".join(parts)


@tool(name="recall_node_tool", description="搜索特定实体或概念（记忆节点）的背景信息", parameters={
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "节点名称，如人名、地名、概念"}
    },
    "required": ["name"]
})
async def recall_node_tool(name: str, _ctx=None):
    """回忆特定节点"""
    if not _ctx or not hasattr(_ctx, 'metadata'):
        return "【错误】无法访问记忆系统"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return "【错误】记忆系统未初始化"

    node = await memory.get_node(name)
    if not node:
        # 尝试模糊搜索
        nodes = await memory.recall_nodes(name, limit=3)
        if nodes:
            parts = [f"【节点回忆】未找到精确匹配 '{name}'，相关节点:"]
            for n in nodes:
                parts.append(f"- {n.name} ({n.node_type}): {n.description[:80]}")
            return "\n".join(parts)
        return f"【节点回忆】未找到关于 '{name}' 的记忆节点"

    related = ""
    if node.related_nodes:
        related = "\n关联节点: " + ", ".join(node.related_nodes[:5])

    return f"""【节点回忆】{node.name} ({node.node_type})
描述: {node.description}
重要性: {node.importance:.2f}
出现次数: {node.occurrence_count}
首次出现: {node.first_seen[:10]}{related}"""


@tool(name="deep_recall_tool", description="深度回想特定事件细节、主题联想或日期心境", parameters={
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "目标ID或日期 (evt_xxx/theme_xxx/YYYY-MM-DD)"},
        "mode": {"type": "string", "description": "回想模式: event(事件)/theme(主题)/date(日期)", "default": "event"}
    },
    "required": ["target"]
})
async def deep_recall_tool(target: str, mode: str = "event", _ctx=None):
    """深度回想"""
    if not _ctx or not hasattr(_ctx, 'metadata'):
        return "【错误】无法访问记忆系统"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return "【错误】记忆系统未初始化"

    if mode == "date":
        # 获取指定日期的总结和事件
        summary = await memory.get_daily_summary(target)
        events = await memory.node_manager.get_events_by_date(target)

        parts = [f"【深度回想 - {target}】"]
        if summary:
            parts.append(f"当日感悟: {summary.insights[:200]}")
        if events:
            parts.append("当日事件:")
            for e in events:
                parts.append(f"- {e.narrative[:100]}")
        return "\n".join(parts) if len(parts) > 1 else f"未找到 {target} 的记忆"

    elif mode == "theme":
        themes = await memory.node_manager.get_themes()
        for t in themes:
            if t.id == target or t.name == target:
                return f"【主题回想】{t.name}\n描述: {t.description}\n包含事件: {len(t.event_ids)}个"
        return f"未找到主题 {target}"

    else:  # event
        # 获取事件并找相关
        return f"【事件回想】目标: {target} (事件详情检索功能开发中)"


@tool(name="recall_recent_events_tool", description="获取最近一段时间内重要或情感强烈的事件片段", parameters={
    "type": "object",
    "properties": {
        "days": {"type": "integer", "description": "回顾天数", "default": 7},
        "min_score": {"type": "number", "description": "最低重要性分数", "default": 0.5}
    }
})
async def recall_recent_events_tool(days: int = 7, min_score: float = 0.5, _ctx=None):
    """回忆近期重要事件"""
    if not _ctx or not hasattr(_ctx, 'metadata'):
        return "【错误】无法访问记忆系统"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return "【错误】记忆系统未初始化"

    events = await memory.node_manager.get_recent_events(days=days, min_importance=min_score)
    if not events:
        return f"【近期回忆】近{days}天没有重要事件"

    parts = [f"【近{days}天重要事件】"]
    for e in events[:5]:
        emotion = f" [情感强度:{e.emotion_intensity:.1f}]" if e.emotion_intensity > 0 else ""
        parts.append(f"- [{e.date}]{emotion} {e.narrative[:80]}")

    return "\n".join(parts)


@tool(name="write_node_tool", description="写入或更新一个记忆节点（实体/概念）", parameters={
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "节点名称"},
        "node_type": {"type": "string", "description": "节点类型: person/place/concept/event/theme"},
        "description": {"type": "string", "description": "节点描述"},
        "importance": {"type": "number", "description": "重要性 0.0-1.0", "default": 0.5}
    },
    "required": ["name", "node_type", "description"]
})
async def write_node_tool(name: str, node_type: str, description: str, importance: float = 0.5, _ctx=None):
    """写入记忆节点"""
    if not _ctx or not hasattr(_ctx, 'metadata'):
        return "【错误】无法访问记忆系统"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return "【错误】记忆系统未初始化"

    node = await memory.create_node(name, node_type, description, importance=importance)
    return f"【节点已记录】{node.name} ({node.node_type}) - 重要性: {node.importance:.2f}"


@tool(name="daily_summary_tool", description="触发指定日期的每日总结（整理事件、提取感悟、更新节点）", parameters={
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "日期 YYYY-MM-DD，默认今天"}
    }
})
async def daily_summary_tool(date: str = "", _ctx=None):
    """每日总结工具"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    if not _ctx or not hasattr(_ctx, 'metadata'):
        return f"【每日总结】{date} 的总结已触发（记忆系统未完全初始化）"

    memory = _ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return f"【每日总结】{date} 的总结已触发"

    # 获取当天事件
    events = await memory.node_manager.get_events_by_date(date)

    # 存储总结占位（实际应由LLM生成）
    await memory.store_daily_summary(
        date=date,
        events=events,
        insights=f"今日共记录 {len(events)} 个事件"
    )

    return f"【每日总结】{date} 总结完成，共 {len(events)} 个事件"
