"""自我完整性校验（调用防御层）。

周期计算自身代码/配置哈希，检测被篡改/注入（memory-mapped 模块异常、导入异常）。
"""
from typing import Dict, Any, List
import os
import sys
import hashlib
import importlib
import time

from .audit import log_event, EventSeverity

# 需要监控的自身文件（相对包目录）
_MONITORED = [
    "__init__.py",
    "process_tree.py",
    "parent_validate.py",
    "integrity.py",
    "audit.py",
    "daemon.py",
    # override.py 决定"谁能关掉防御"。不纳入监控的话，
    # 把它改成 _require_owner 恒真，integrity_status() 仍会报 ok。
    "override.py",
]

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_snapshot: Dict[str, str] = {}


def _file_hash(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _snapshot_own_files() -> Dict[str, str]:
    snap = {}
    for name in _MONITORED:
        p = os.path.join(_PACKAGE_DIR, name)
        if os.path.isfile(p):
            snap[name] = _file_hash(p)
    return snap


def _check_module_integrity() -> List[str]:
    """检测当前已加载的 call_defense 子模块是否与磁盘一致。"""
    issues = []
    for modname in _MONITORED:
        if modname == "__init__.py":
            full = "elysia_agent.core.call_defense"
        else:
            full = "elysia_agent.core.call_defense." + modname[:-3]
        mod = sys.modules.get(full)
        if mod is None:
            continue
        file = getattr(mod, "__file__", None)
        if not file or not os.path.isfile(file):
            issues.append(f"{modname}: no file (in-memory injection?)")
    return issues


def _ensure_snapshot() -> None:
    global _snapshot
    if not _snapshot:
        _snapshot = _snapshot_own_files()


def integrity_status() -> Dict[str, Any]:
    """返回自我完整性校验结果，并与首次快照对比检测篡改。"""
    global _snapshot
    _ensure_snapshot()
    current = _snapshot_own_files()
    tampered = []
    for name, h in current.items():
        if name in _snapshot and _snapshot[name] != h:
            tampered.append(name)
    missing = [name for name in _MONITORED if name not in current]

    mem_issues = _check_module_integrity()

    ok = not tampered and not missing and not mem_issues
    if tampered:
        log_event(EventSeverity.CRITICAL, "integrity",
                  "调用防御层自身文件被篡改", detail={"files": tampered})
    if mem_issues:
        log_event(EventSeverity.CRITICAL, "integrity",
                  "检测到内存注入模块", detail={"modules": mem_issues})

    return {
        "ok": ok,
        "tampered_files": tampered,
        "missing_files": missing,
        "memory_issues": mem_issues,
        "monitored_files": len(current),
        "checked_at": time.time(),
    }
