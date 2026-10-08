"""调用防御层（独立进程）v2.4.0

阶段二：调用防御独立化
- 进程树监控：捕获主进程带动的一切子进程，记录 pid/ppid/启动参数/二进制哈希。
- 父进程校验：检查主进程的父进程是否为预期启动者（非异常/非被注入宿主）。
- 自我完整性校验：周期计算自身代码/配置哈希，检测被篡改/注入。
- 提供 /call_defense status 查询，异常事件写安全日志。

作为独立进程/守护进程运行，主进程崩溃不影响，可单独启停。
"""
from typing import Dict, Any

from .daemon import CallDefenseDaemon, get_daemon_singleton
from .process_tree import process_tree_snapshot, is_trusted_ppid
from .parent_validate import validate_main_parent
from .integrity import integrity_status
from .audit import SecurityLog, EventSeverity, log_event, get_events

__all__ = [
    "CallDefenseDaemon",
    "get_daemon_singleton",
    "process_tree_snapshot",
    "is_trusted_ppid",
    "validate_main_parent",
    "integrity_status",
    "SecurityLog",
    "EventSeverity",
    "log_event",
    "get_events",
]

__version__ = "2.4.0"


def status() -> Dict[str, Any]:
    """汇总调用防御层当前状态，供 /call_defense status 查询。"""
    return {
        "module": "call_defense",
        "version": __version__,
        "daemon_running": get_daemon_singleton().running,
        "daemon_pid": get_daemon_singleton().pid,
        "process_tree": process_tree_snapshot(),
        "parent_validation": validate_main_parent(),
        "integrity": integrity_status(),
        "recent_events": get_events(limit=20),
    }
