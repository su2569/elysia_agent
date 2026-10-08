"""安全审计日志（调用防御层）。

异常事件统一写安全日志，供 /call_defense status 与后续阶段查询。
"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import os
import time
import json
import threading

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)
_EVENTS_FILE = os.path.join(_DATA_DIR, "call_defense_events.jsonl")


class EventSeverity(Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class SecurityLog:
    """单条安全事件日志。"""
    ts: float = 0.0
    severity: str = "info"
    category: str = ""
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts)),
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "detail": self.detail,
        }


_lock = threading.Lock()
_inmem: List[SecurityLog] = []


def _ensure_dir() -> None:
    if _DATA_DIR and not os.path.isdir(_DATA_DIR):
        os.makedirs(_DATA_DIR, exist_ok=True)


def _append_to_file(rec: Dict[str, Any]) -> None:
    try:
        _ensure_dir()
        with open(_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_event(severity: EventSeverity, category: str, message: str,
              detail: Optional[Dict[str, Any]] = None) -> SecurityLog:
    """记录一条安全事件：写内存环形缓冲 + 落盘 jsonl。"""
    sev = severity.value if isinstance(severity, EventSeverity) else str(severity)
    ev = SecurityLog(
        ts=time.time(),
        severity=sev,
        category=category,
        message=message,
        detail=detail or {},
    )
    with _lock:
        _inmem.append(ev)
        if len(_inmem) > 200:
            del _inmem[0]
    _append_to_file(ev.to_dict())
    return ev


def get_events(limit: int = 50, severity: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回最近的安全事件（可选按严重级别过滤）。"""
    with _lock:
        items = list(_inmem)
    if severity:
        items = [e for e in items if e.severity == severity]
    return [e.to_dict() for e in items[-limit:]]
