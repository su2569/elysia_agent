"""父进程校验（调用防御层）。

检查主进程的父进程是否为预期启动者（非异常/非被注入宿主）。
异常 ppid 触发告警并写安全日志。
"""
from typing import Dict, Any, Optional
import os
import sys

from .audit import log_event, EventSeverity
from .process_tree import _process_list, is_trusted_ppid

# 预期的主进程名称（可通过配置覆盖）
EXPECTED_MAIN = {"python", "pythonw", "python.exe", "pythonw.exe", "py.exe"}


def validate_main_parent(main_pid: Optional[int] = None) -> Dict[str, Any]:
    """校验主进程的父进程是否可信。返回校验结果字典。"""
    if main_pid is None:
        main_pid = os.getpid()

    procs = {p["pid"]: p for p in _process_list()}
    main = procs.get(main_pid)
    if not main:
        return {"ok": False, "error": "main process not found"}

    ppid = main.get("ppid", 0)
    parent = procs.get(ppid)

    result = {
        "main_pid": main_pid,
        "main_name": main.get("name", ""),
        "ppid": ppid,
        "parent_name": parent.get("name", "") if parent else None,
        "ok": False,
        "reason": "",
    }

    if ppid <= 0:
        result["ok"] = True
        result["reason"] = "system/kernel parent (trusted)"
        return result

    if parent is None:
        result["reason"] = "parent process not found (possible injection)"
        log_event(EventSeverity.CRITICAL, "parent_validation",
                  "主进程的父进程无法定位，疑似被注入/替换",
                  detail={"ppid": ppid})
        return result

    pname = parent.get("name", "").lower()
    if pname in EXPECTED_MAIN:
        result["ok"] = True
        result["reason"] = f"expected launcher: {parent.get('name')}"
    else:
        result["reason"] = f"unexpected parent: {parent.get('name')}"
        log_event(EventSeverity.WARN, "parent_validation",
                  "检测到异常父进程，非预期启动者",
                  detail={"ppid": ppid, "parent": parent.get("name")})
    return result
