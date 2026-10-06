"""Built-in Hooks - 参考 APLR 的自动回忆机制

提供：
- 新会话自动历史注入
- 聊天时自动回忆
- 记忆节点唤起
"""
from ..decorators import hook
from ..core.state import Priority
from ..memory import EnhancedMemory
from ..utils.logger import get_logger

logger = get_logger("BuiltinHooks")

@hook("on_start", priority=Priority.HIGH)
async def auto_inject_history(ctx, **kwargs):
    """
    新会话自动历史注入（APLR风格）

    检测到新会话时，自动注入：
    - 近3天重要事件
    - 近7天每日总结
    """
    memory = ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return

    # 检查是否为新会话（step_count == 0 且没有历史）
    if ctx.step_count == 0 and not ctx.injected_pre.get("_history_injected"):
        history_context = memory.get_new_session_context(
            days_important=3,
            days_summary=7
        )

        if history_context:
            ctx.injected_pre["_history_injected"] = True
            ctx.injected_pre["session_history"] = history_context
            logger.info(f"Auto-injected history for user {ctx.user_id}")

@hook("before_prepare", priority=Priority.NORMAL)
async def auto_recall_on_chat(ctx, **kwargs):
    """
    聊天时自动回忆（APLR风格）

    根据用户输入自动检索相关记忆节点和事件
    """
    memory = ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return

    if not ctx.query:
        return

    # 自动回忆相关记忆
    recall_results = memory.auto_recall(ctx.query, top_k=3)

    if recall_results:
        # 将回忆结果注入上下文
        ctx.injected_pre["auto_recall"] = recall_results

        # 同时检索记忆节点
        nodes = memory.recall_nodes(ctx.query, limit=3)
        if nodes:
            ctx.injected_pre["recalled_nodes"] = [
                {"name": n.name, "type": n.node_type, "desc": n.description[:50]}
                for n in nodes
            ]

        logger.info(f"Auto-recall triggered for query: {ctx.query[:30]}...")

@hook("after_finalize", priority=Priority.LOW)
async def extract_nodes_from_conversation(ctx, **kwargs):
    """
    对话结束后自动提取记忆节点

    从对话内容中提取人物、地点、概念等节点
    """
    memory = ctx.metadata.get("_memory")
    if not memory or not isinstance(memory, EnhancedMemory):
        return

    # 简化版：如果对话中提到了"记住"或"我是"，尝试提取节点
    full_text = f"{ctx.query} {ctx.final_answer}"

    # 简单的节点提取启发式（实际应由LLM完成）
    import re

    # 提取 "我是XXX" 或 "我叫XXX" -> person 节点
    name_match = re.search(r"我[是叫]([\u4e00-\u9fa5]{2,8})", full_text)
    if name_match:
        name = name_match.group(1)
        memory.create_node(
            name=name,
            node_type="person",
            description=f"用户提到自己是{name}",
            importance=0.8
        )
        logger.info(f"Auto-extracted person node: {name}")

    # 提取地点
    place_match = re.search(r"在([\u4e00-\u9fa5]{2,10})(?:市|省|县|区|镇|村)?", full_text)
    if place_match:
        place = place_match.group(1)
        memory.create_node(
            name=place,
            node_type="place",
            description=f"提到的地点",
            importance=0.5
        )
