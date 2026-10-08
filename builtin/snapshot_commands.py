"""快照回滚管理命令（阶段一）

独立挂载到 CommandHandler，不修改内置命令列表，保持向后兼容。

用法（在 runner 初始化处）:
    from elysia_agent.builtin.snapshot_commands import install_snapshot_commands
    handler = get_command_handler(runner)
    install_snapshot_commands(handler)

可用命令:
    /snap status          - 查看快照系统状态
    /snap list [路径]     - 查看被监控文件的快照历史
    /snap rollback [n]    - 回滚被修改文件到前 n 轮（默认 1，最大 3）
"""
import time
import logging
from typing import Optional

from ..core.sandbox_snapshot import snap
from ..core.state import Priority
from ..interfaces.hook import IHook
from .commands import CommandContext

logger = logging.getLogger("SnapshotCommands")


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "?"


async def _cmd_snap_status(ctx: CommandContext) -> str:
    st = snap.status()
    lines = ["快照回滚系统状态"]
    lines.append(f"  启用: {'是' if st['active'] else '否'}")
    lines.append(f"  轮数深度: {st['depth']}")
    lines.append(f"  已快照文件: {st['snapshotted_files']}")
    lines.append(f"  累计回滚: {st['restore_count']}")
    lines.append(f"  备份目录: {st['backup_dir'] or '（内存）'}")
    if not st["roots"]:
        lines.append("  监控根: （空，未设置）")
    else:
        lines.append("  监控根:")
        for root, info in st["roots"].items():
            lines.append(f"    {root}  (tracked {info['tracked']}/{info['total']})")
    return "\n".join(lines)


async def _cmd_snap_list(ctx: CommandContext) -> str:
    query = " ".join(ctx.parts[1:]) if ctx.parts else ""
    hist = snap.history(query)
    if not hist:
        return "未发现已快照文件" + (f"（匹配: {query}）" if query else "")
    lines = ["文件快照历史（n 越大越旧）:"]
    for item in hist[:40]:
        revs = ", ".join(
            f"n{r['n']}@{_fmt_ts(r['ts'])}" + ("" if r['has_content'] else "(哈希only)")
            for r in item["revisions"]
        )
        lines.append(f"  {item['file']}")
        lines.append(f"      {revs}")
    return "\n".join(lines)


async def _cmd_snap_rollback(ctx: CommandContext) -> str:
    if not snap.is_active():
        return "快照系统未激活，无法回滚。"
    n = 1
    if len(ctx.parts) > 1:
        try:
            n = int(ctx.parts[1])
        except ValueError:
            n = 1
    if n < 1:
        n = 1
    summary = snap.rollback(n)
    restored = summary["restored"]
    failed = summary["failed"]
    skipped = summary["skipped"]
    lines = [f"回滚执行完成（回滚到前 {n} 轮）"]
    lines.append(f"  已恢复 {len(restored)} 个文件")
    for f in restored[:20]:
        lines.append(f"    ✓ {f}")
    if len(restored) > 20:
        lines.append(f"    … 等共 {len(restored)} 个")
    if skipped:
        lines.append(f"  跳过 {len(skipped)} 个（无可用内容备份或未被修改）")
    if failed:
        lines.append(f"  失败 {len(failed)} 个:")
        for f, err in failed[:10]:
            lines.append(f"    ✗ {f}: {err}")
    return "\n".join(lines)


def install_snapshot_commands(handler) -> None:
    """把快照命令挂到 CommandHandler（不修改内置命令列表）"""
    handler.register("snap", _cmd_snap_status, "快照回滚系统状态（/snap status|list|rollback）", hidden=False)
    # 为便于 /help 展示，仍用 /snap 统一入口；子命令在内部解析。
    logger.info("快照回滚命令已安装（/snap status|list|rollback）")


# ==================== before_act 快照钩子 ====================

class SnapshotBeforeActHook(IHook):
    """每次工具执行前，对监控范围内的文件打一轮快照。

    这样若工具（如写文件/改配置）改坏了被监控文件，可在之后
    /snap rollback 恢复到该工具执行前的状态。
    """

    @property
    def event(self) -> str:
        return "before_act"

    @property
    def priority(self) -> Priority:
        return Priority.HIGH

    @property
    def name(self) -> str:
        return "snapshot_before_act"

    async def trigger(self, ctx, **kwargs) -> None:
        action = kwargs.get("action", "")
        if not snap.is_active():
            return
        # 只对可能产生写操作的执行打点（其余工具打点无意义，浪费轮次）
        WRITE_HINTS = ("write", "edit", "create", "delete", "save", "update",
                       "set", "config", "install", "remove", "patch", "file")
        if not any(h in action.lower() for h in WRITE_HINTS):
            return
        try:
            # 对每个监控根打一轮快照（使用公开 snapshot API）
            for root in list(snap.monitor):
                snap.snapshot(root, reason=f"pre:{action}")
        except Exception as e:
            logger.warning(f"Snapshot hook error: {e}")


def install_snapshot_hooks() -> None:
    """注册 before_act 快照钩子到全局 HookRegistry（幂等）"""
    from ..registry.hook_registry import HookRegistry
    HookRegistry.register(SnapshotBeforeActHook())
    logger.info("快照 before_act 钩子已注册（工具执行前自动存档）")