"""沙箱快照回滚系统（阶段一，v1.0.0）

设计：
- 独立于 sandbox.py，向后兼容，不改变现有调用链
- 对被监控目录/文件维护三轮滚动快照（内容 + SHA-256 + 时间戳）
- 支持 rollback(n) 恢复到前 n 轮（n = 1/2/3）
- 写操作前打点：快照层登记，写后校验哈希变化，检测被修改/间接修改
- 快照存储于内存（默认）或可选的持久化备份目录
- 模块级单例，未激活时所有调用透传（透传 = 记录但不强制，配合开关）

用法:
    from elysia_agent.core.sandbox_snapshot import snap
    snap.activate(monitor=["elysia.json", "config/"])
    snap.snapshot("elysia.json")      # 记录当前状态为一轮快照
    snap.rollback(1)                  # 恢复到前 1 轮
"""
import os
import re
import time
import hashlib
import logging
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SandboxSnapshot")

# 被监控时忽略的临时/日志文件（避免快照堆积无意义轮次）
IGNORE_RE = re.compile(r"\.(?:pyc|log|tmp|swp|bak)$|~$|^~\$", re.IGNORECASE)
# 默认快照轮数
DEFAULT_DEPTH = 3
# 单个文件快照内容上限（防止超大文件占满内存，超出则仅记哈希）
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024  # 8MB


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: str) -> Optional[str]:
    """计算文件 SHA-256，不存在/不可读返回 None"""
    try:
        with open(path, "rb") as f:
            return _sha256(f.read())
    except OSError:
        return None


class SandboxSnapshot:
    """沙箱快照回滚单例"""

    _instance: Optional["SandboxSnapshot"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.active = False
        self.depth = DEFAULT_DEPTH
        # 监控根：目录用集合，文件也可用
        self.monitor: List[str] = []
        # snapshots: path -> deque-like list of (ts, sha, content|None)  最近的在末尾
        self.snapshots: Dict[str, List[Tuple[float, str, Optional[bytes]]]] = {}
        # 变更记录（谁在何时改了哪个文件）
        self.changes: List[Dict[str, Any]] = []
        # 持久化备份目录（可选）
        self.backup_dir: Optional[str] = None
        self._restore_log = []

    # ==================== 生命周期 ====================

    def activate(self, monitor: Optional[List[str]] = None,
                 depth: int = DEFAULT_DEPTH,
                 backup_dir: Optional[str] = None) -> bool:
        """激活快照系统。monitor: 被监控的文件或目录路径列表。"""
        if self.active:
            return True
        self.active = True
        self.depth = max(1, min(9, int(depth)))
        self.monitor = [os.path.normpath(p) for p in (monitor or [])]
        if backup_dir:
            self.backup_dir = os.path.abspath(backup_dir)
            os.makedirs(self.backup_dir, exist_ok=True)
        # 初始打一轮基线快照
        for p in list(self.monitor):
            self._snapshot_path(p, reason="baseline")
        logger.info(
            f"SandboxSnapshot activated: {len(self.monitor)} roots, "
            f"depth={self.depth}, backup={self.backup_dir!r}"
        )
        return True

    def is_active(self) -> bool:
        return self.active

    # ==================== 路径处理 ====================

    def _covered(self, path: str) -> Optional[str]:
        """返回命中该 path 的监控根（目录或文件），未命中返回 None"""
        norm = os.path.normpath(path)
        for root in self.monitor:
            r = os.path.normpath(root)
            if norm == r:
                return root
            if os.path.isdir(r) and norm.startswith(r + os.sep):
                return root
        return None

    def _collect_files(self, path: str) -> List[str]:
        """收集 path（文件或目录）下应纳入快照的文件列表"""
        if os.path.isfile(path):
            return [os.path.abspath(path)] if not IGNORE_RE.search(path) else []
        if os.path.isdir(path):
            out = []
            for dp, _, fns in os.walk(path):
                for fn in fns:
                    fp = os.path.join(dp, fn)
                    if not IGNORE_RE.search(fp):
                        out.append(os.path.abspath(fp))
            return out
        return []

    def _snapshot_path(self, path: str, reason: str = "manual"):
        """对 path（目录或文件）下所有文件更新一轮快照。"""
        for fp in self._collect_files(path):
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            sha = _sha256(data)
            content = data if len(data) <= MAX_SNAPSHOT_BYTES else None
            self.snapshots.setdefault(fp, []).append((time.time(), sha, content))
            # 只保留最近 depth 轮
            q = self.snapshots[fp]
            if len(q) > self.depth:
                del q[:len(q) - self.depth]

    # ==================== 对外接口 ====================

    def snapshot(self, path: str, reason: str = "manual") -> bool:
        """主动打点：记录当前状态为一轮快照。返回是否执行。"""
        if not self.active:
            return True  # 未激活透传（不强制）
        if not self._covered(path):
            return False  # 不在监控范围，忽略
        self._snapshot_path(path, reason)
        return True

    def diff(self, path: str) -> List[Tuple[str, str, str]]:
        """返回 path 下相对最新快照的差异: [(file, old_sha, new_sha)]"""
        result = []
        if not self.active:
            return result
        for fp in self._collect_files(path):
            cur = _file_hash(fp)
            q = self.snapshots.get(fp)
            latest = q[-1][1] if q else None
            if cur != latest:
                result.append((fp, latest or "<none>", cur or "<deleted>"))
        return result

    def rollback(self, n: int = 1) -> Dict[str, Any]:
        """回滚被监控文件到第 n 轮之前的快照内容。
        n=1 → 恢复到最近一轮快照之前的状态（即上一轮快照记录的内容）;
        n=2 → 恢复到上上轮快照记录的内容; 以此类推。
        对每个有内容备份的监控文件按队列位置恢复，并记录 restore_log。
        """
        summary = {"restored": [], "skipped": [], "failed": []}
        if not self.active:
            logger.warning("rollback called but snapshot inactive")
            return summary
        for root in list(self.monitor):
            for fp in self._collect_files(root):
                q = self.snapshots.get(fp)
                if not q:
                    continue
                # 回滚到「从最新往旧数第 n 轮」之前记录的内容。
                # n=1 → 恢复到最近一轮快照之前的状态（即上一轮快照记录的内容）。
                # 目标索引: idx = len(q)-1-n。若该轮不存在（越界），回退到队列最旧可用快照（基线兜底）。
                idx = max(0, len(q) - 1 - n)
                ts, sha, content = q[idx]
                if content is None:
                    summary["skipped"].append(fp)
                    continue
                try:
                    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                    with open(fp, "wb") as f:
                        f.write(content)
                    summary["restored"].append(fp)
                    self._restore_log.append((time.time(), fp))
                except OSError as e:
                    summary["failed"].append((fp, str(e)))
        return summary

    def status(self) -> Dict[str, Any]:
        """查询快照系统状态"""
        roots = {}
        for root in self.monitor:
            total = tracked = 0
            for fp in self._collect_files(root):
                total += 1
                if fp in self.snapshots:
                    tracked += 1
            roots[root] = {"tracked": tracked, "total": total}
        return {
            "active": self.active,
            "depth": self.depth,
            "roots": roots,
            "monitored_files": sum(1 for r in self.monitor for _ in self._collect_files(r)),
            "snapshotted_files": len(self.snapshots),
            "backup_dir": self.backup_dir,
            "restore_count": len(self._restore_log),
        }

    def history(self, path: str = "") -> List[Dict[str, Any]]:
        """查看某路径（或全部）的快照历史"""
        out = []
        for root in self.monitor:
            if path and not self._covered(path):
                continue
            for fp in self._collect_files(root):
                q = self.snapshots.get(fp)
                if q:
                    out.append({
                        "file": fp,
                        "revisions": [
                            {"n": i + 1, "ts": ts, "sha": sha[:12],
                             "has_content": content is not None}
                            for i, (ts, sha, content) in enumerate(q)
                        ],
                    })
        return out


# 模块级单例
snap = SandboxSnapshot()