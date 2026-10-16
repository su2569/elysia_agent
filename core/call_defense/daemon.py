"""调用防御守护进程（独立进程）v2.4.0。

作为独立进程运行，周期执行：
- 进程树监控（记录主进程带动的一切子进程）
- 父进程校验
- 自我完整性校验
- 异常写安全日志

主进程崩溃不影响本守护；可通过 /call_defense 单独查询与启停。
"""
from typing import Dict, Any, Optional
import os
import sys
import time
import threading

from .audit import log_event, EventSeverity
from .process_tree import process_tree_snapshot
from .parent_validate import validate_main_parent
from .integrity import integrity_status
from .override import override_status  # 阶段四：心跳上报 + 豁免超时自动恢复


class CallDefenseDaemon:
    """调用防御守护进程（线程模式，可在主进程内作为守护线程运行）。"""

    def __init__(self, interval: float = 10.0, main_pid: Optional[int] = None):
        self.interval = interval
        self.main_pid = main_pid if main_pid is not None else os.getpid()
        self.running = False
        self.pid = os.getpid()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_scan: Dict[str, Any] = {}

    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="CallDefenseDaemon")
        self._thread.start()
        log_event(EventSeverity.INFO, "daemon", "调用防御守护进程启动",
                  detail={"interval": self.interval})
        return True

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(self.interval)

    def scan_once(self) -> Dict[str, Any]:
        """执行一轮扫描，返回结果并记录异常。"""
        result = {
            "ts": time.time(),
            "process_tree": process_tree_snapshot(self.main_pid),
            "parent": validate_main_parent(self.main_pid),
            "integrity": integrity_status(),
            "override": override_status(),  # 阶段四：豁免状态 + 心跳
        }
        self.last_scan = result

        # 记录异常事件
        if not result["parent"].get("ok", True):
            log_event(EventSeverity.WARN, "parent_validation",
                      "父进程校验未通过", detail=result["parent"])
        if not result["integrity"].get("ok", True):
            log_event(EventSeverity.CRITICAL, "integrity",
                      "完整性校验未通过", detail=result["integrity"])
        return result


_singleton: Optional[CallDefenseDaemon] = None
_singleton_lock = threading.Lock()


def get_daemon_singleton() -> CallDefenseDaemon:
    """返回全局单例守护实例（惰性初始化）。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = CallDefenseDaemon()
    return _singleton


def start_daemon() -> CallDefenseDaemon:
    """启动守护（若未运行）。供主程序调用。"""
    d = get_daemon_singleton()
    d.start()
    return d


def stop_daemon() -> None:
    get_daemon_singleton().stop()
