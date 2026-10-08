"""进程树监控（调用防御层）。

捕获主进程带动的一切子进程，记录 pid/ppid/启动参数/二进制哈希。
提供快照供 status 查询。
"""
from typing import Dict, Any, List, Optional
import os
import sys
import time

TRUSTED_LAUNCHERS = {"python", "pythonw", "python.exe", "pythonw.exe", "py.exe", "uv.exe"}


def _hash_file(path: str) -> str:
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _process_list() -> List[Dict[str, Any]]:
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "ppid", "name", "exe", "cmdline"]):
            try:
                info = p.info
                out.append({
                    "pid": info.get("pid", 0),
                    "ppid": info.get("ppid", 0) or 0,
                    "name": info.get("name", "") or "",
                    "exe": info.get("exe", "") or "",
                    "cmdline": " ".join(info.get("cmdline") or []) if info.get("cmdline") else "",
                })
            except Exception:
                continue
    except ImportError:
        if sys.platform.startswith("win"):
            try:
                import subprocess
                r = subprocess.run(
                    ["wmic", "process", "get", "ProcessId,ParentProcessId,Name,ExecutablePath",
                     "/format:csv"],
                    capture_output=True, text=True, timeout=10)
                for line in r.stdout.splitlines()[1:]:
                    parts = line.split(",", 6)
                    if len(parts) < 5:
                        continue
                    name, pid, ppid, exe = parts[1].strip(), parts[2].strip(), parts[3].strip(), parts[4].strip()
                    if pid.isdigit():
                        out.append({
                            "pid": int(pid),
                            "ppid": int(ppid) if ppid.isdigit() else 0,
                            "name": name,
                            "exe": exe,
                            "cmdline": "",
                        })
            except Exception:
                pass
    return out


def is_trusted_ppid(ppid: int) -> bool:
    """判断 ppid 是否属于受信任的启动进程。"""
    if ppid <= 0:
        return True
    procs = {p["pid"]: p for p in _process_list()}
    p = procs.get(ppid)
    if not p:
        return False
    return p["name"].lower() in TRUSTED_LAUNCHERS


def process_tree_snapshot(root_pid: Optional[int] = None) -> Dict[str, Any]:
    """生成以 root_pid 为根的进程树快照。"""
    if root_pid is None:
        root_pid = os.getpid()
    procs = _process_list()
    by_ppid: Dict[int, List[Dict[str, Any]]] = {}
    for p in procs:
        by_ppid.setdefault(p["ppid"], []).append(p)

    tree: List[Dict[str, Any]] = []

    def walk(pid: int, depth: int) -> None:
        for child in by_ppid.get(pid, []):
            child["depth"] = depth
            child["hash"] = _hash_file(child.get("exe", "")) if child.get("exe") else ""
            tree.append(child)
            walk(child["pid"], depth + 1)

    walk(root_pid, 0)
    return {
        "root_pid": root_pid,
        "count": len(tree),
        "snapshot_time": time.time(),
        "children": tree,
    }
