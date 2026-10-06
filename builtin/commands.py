"""内置命令系统（v2.5.0，模仿 AstrBot 命令体系）

以 / 开头的消息由命令处理器优先拦截，返回命令结果而不进入 Agent 循环。
命令处理器为可选能力：未 install 时 runner 行为完全不变（纯调用式）。

用法:
    from elysia_agent.builtin.commands import install
    install(runner)          # 挂载到 runner（runner.install_commands()）

内置命令:
    /help /status /stats /memory /clear /reset /guard /version

自定义命令:
    handler.register("mytool", lambda ctx: "结果", "我的工具")
"""
import time
from typing import Dict, Any, Optional, Callable, Awaitable

from ..utils.logger import get_logger

logger = get_logger("Commands")

# 命令前缀（与 Telegram 等平台命令前缀一致）
PREFIX = "/"


class CommandContext:
    """命令上下文：命令处理器收到的参数"""

    def __init__(self, user_id: str, query: str, group_id: Optional[str] = None,
                 platform: str = "unknown", runner=None):
        self.user_id = user_id
        self.query = query
        self.group_id = group_id
        self.platform = platform
        self.runner = runner

    @property
    def parts(self) -> list:
        """['cmd', 'arg1', 'arg2', ...]"""
        return self.query.strip().split()


class CommandHandler:
    """命令处理器（模仿 AstrBot 的命令注册/匹配机制）"""

    def __init__(self, runner=None, prefix: str = PREFIX):
        self.runner = runner
        self.prefix = prefix
        self._commands: Dict[str, Dict[str, Any]] = {}
        # 注册内置命令
        for name, fn, desc in self._builtin_commands():
            self.register(name, fn, desc)

    # ==================== 注册 ====================

    def register(self, name: str, fn: Callable[[CommandContext], Awaitable[Optional[str]]],
                 description: str = "", hidden: bool = False) -> None:
        """注册命令

        fn: async (ctx: CommandContext) -> Optional[str]，返回 None 表示不处理。
        """
        self._commands[name.lstrip(self.prefix)] = {
            "fn": fn, "description": description, "hidden": hidden,
        }

    def unregister(self, name: str) -> bool:
        key = name.lstrip(self.prefix)
        if key in self._commands and key in ("help", "status", "stats",
                                             "memory", "clear", "reset", "guard", "version"):
            # 内置命令不允许注销（保证 /help 永远可用）
            return False
        return self._commands.pop(key, None) is not None

    def list_commands(self) -> list:
        return [
            {"name": "/" + name, "description": info["description"]}
            for name, info in sorted(self._commands.items())
            if not info["hidden"]
        ]

    # ==================== 分发 ====================

    async def handle(self, user_id: str, query: str, group_id: Optional[str] = None,
                     platform: str = "unknown") -> Optional[Dict[str, Any]]:
        """
        尝试处理命令消息。

        Returns:
            None 表示非命令/未命中；否则返回 runner.run() 同构的结果字典。
        """
        stripped = query.strip()
        if not stripped.startswith(self.prefix):
            return None

        parts = stripped.split()
        cmd_name = parts[0][len(self.prefix):].lower()
        info = self._commands.get(cmd_name)
        if info is None:
            return {
                "text": f"未知命令: /{cmd_name}（输入 /help 查看可用命令）",
                "success": False, "error": f"unknown_command: {cmd_name}",
                "is_command": True,
            }

        ctx = CommandContext(user_id=user_id, query=stripped, group_id=group_id,
                             platform=platform, runner=self.runner)
        try:
            reply = await info["fn"](ctx)
        except Exception as e:
            logger.error(f"命令 /{cmd_name} 执行异常（进程继续运行）: {e}")
            return {
                "text": f"命令执行异常: {e}", "success": False,
                "error": f"command_error: {e}", "is_command": True,
            }

        if reply is None:
            return None
        return {"text": reply, "success": True, "is_command": True}

    # ==================== 内置命令 ====================

    def _builtin_commands(self) -> list:
        return [
            ("help", self._cmd_help, "显示可用命令列表"),
            ("status", self._cmd_status, "查看运行状态"),
            ("stats", self._cmd_stats, "查看运行统计"),
            ("memory", self._cmd_memory, "查看记忆概览"),
            ("clear", self._cmd_clear, "清空我的记忆"),
            ("reset", self._cmd_reset, "重置当前会话"),
            ("guard", self._cmd_guard, "查看防护状态"),
            ("version", self._cmd_version, "查看版本信息"),
        ]

    async def _cmd_help(self, ctx: CommandContext) -> str:
        lines = ["可用命令："]
        for cmd in self.list_commands():
            lines.append(f"  {cmd['name']} - {cmd['description']}")
        return "\n".join(lines)

    async def _cmd_status(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        lines = ["Elysia Agent 运行状态"]
        if runner is not None:
            lines.append(f"  版本: {getattr(runner, 'VERSION', 'unknown')}")
            lines.append(f"  记忆: {'启用' if getattr(runner, 'memory', None) else '关闭'}")
            lines.append(f"  防护: {'启用' if getattr(runner, 'guard', None) else '关闭'}")
            lines.append(f"  情绪: {'启用' if getattr(runner, 'emotion', None) else '关闭'}")
        if ctx.group_id:
            lines.append(f"  群组: {ctx.group_id}")
        lines.append(f"  平台: {ctx.platform}")
        return "\n".join(lines)

    async def _cmd_stats(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        if runner is None or not hasattr(runner, "stats"):
            return "统计不可用"
        s = runner.stats
        return (
            f"运行统计\n"
            f"  总调用: {s.get('total_runs', 0)}\n"
            f"  总错误: {s.get('total_errors', 0)}\n"
            f"  错误率: {s.get('error_rate', 0) * 100:.1f}%\n"
            f"  防护拦截: {s.get('blocked_by_guard', 0)}"
        )

    async def _cmd_memory(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        if runner is None or runner.memory is None:
            return "记忆系统未启用"
        try:
            mem = await runner.memory.retrieve(
                user_id=ctx.user_id, query="", group_id=ctx.group_id,
                include_recent=5, include_semantic=0,
            )
            recent = mem.get("recent", [])
            if not recent:
                return "暂无记忆"
            lines = ["最近记忆："]
            for m in recent[-5:]:
                content = (m.get("content") or m.get("query") or "")[:60]
                lines.append(f"  - {content}")
            return "\n".join(lines)
        except Exception as e:
            return f"记忆读取失败: {e}"

    async def _cmd_clear(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        if runner is None or runner.memory is None:
            return "记忆系统未启用"
        try:
            results = await runner.memory.search(
                query="", search_type="time", limit=500,
                group_id=ctx.group_id,
            )
            count = 0
            for m in results:
                mid = m.get("id") or m.get("memory_id")
                if mid:
                    try:
                        if await runner.memory.delete(mid):
                            count += 1
                    except Exception:
                        pass
            return f"已清空 {count} 条记忆"
        except Exception as e:
            return f"清空失败: {e}"

    async def _cmd_reset(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        if runner is None or runner.memory is None:
            return "会话已重置（无记忆系统）"
        try:
            # 删除最近会话记录（retrieve 拿到的 session 历史来自 recent，重置即清 recent 级记忆）
            results = await runner.memory.search(
                query="", search_type="time", limit=50,
                group_id=ctx.group_id,
            )
            count = 0
            for m in results[:20]:
                mid = m.get("id") or m.get("memory_id")
                if mid:
                    try:
                        if await runner.memory.delete(mid):
                            count += 1
                    except Exception:
                        pass
            return f"会话已重置（清理 {count} 条近期记录）"
        except Exception as e:
            return f"重置失败: {e}"

    async def _cmd_guard(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        if runner is None or runner.guard is None:
            return "提示词防护未启用"
        try:
            stats = runner.guard.get_stats()
        except Exception:
            stats = {}
        return (
            f"提示词防护\n"
            f"  模式: {getattr(runner.guard, 'mode', 'intercept')}\n"
            f"  拦截次数: {stats.get('total_blocked', stats.get('blocked', 0))}\n"
            f"  审查次数: {stats.get('total_checks', stats.get('checks', 0))}"
        )

    async def _cmd_version(self, ctx: CommandContext) -> str:
        runner = ctx.runner
        ver = getattr(runner, "VERSION", "unknown") if runner is not None else "unknown"
        return f"Elysia Agent {ver}（测试版）"


def install(runner) -> CommandHandler:
    """安装内置命令到 runner（幂等）

    runner.run() 会优先拦截以 / 开头的命令消息。
    """
    handler = getattr(runner, "_command_handler", None)
    if handler is not None:
        return handler
    handler = CommandHandler(runner=runner)
    runner.install_command_handler(handler)
    logger.info("内置命令已安装（/help 查看）")
    return handler