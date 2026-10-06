"""程序异常自检（v2.4.0）

- 反未知漏洞利用：检测路径穿越、命令注入、SSRF 等常见漏洞模式
- 反未授权异常行为：工具越权调用、sys.path 污染、可疑模块注入
- 启动自检：配置完整性 / 依赖可用性 / 数据库健康 / 目录权限
- 运行期监控：每次工具调用前检查（block_unauthorized 时拦截）
- 报告输出：data/self_check_report.json
"""
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
import os
import sys
import time
import json
import re
import importlib

from ..utils.logger import get_logger

logger = get_logger("SelfCheck")


@dataclass
class CheckItem:
    """单项自检结果"""
    name: str
    passed: bool
    detail: str = ""
    severity: str = "info"  # info / warn / critical


@dataclass
class SelfCheckReport:
    """自检报告"""
    timestamp: float = 0.0
    checks: List[CheckItem] = field(default_factory=list)
    blocked_events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def critical_issues(self) -> List[CheckItem]:
        return [c for c in self.checks if not c.passed and c.severity == "critical"]


# ==================== 漏洞模式检测 ====================

# 路径穿越（..%2f / ..\\ / ../）
PATH_TRAVERSAL_RE = re.compile(r'(\.\.\s*[\\/]|\.\.%2f|\.\.%5c)', re.IGNORECASE)
# 命令注入分隔符
CMD_INJECTION_RE = re.compile(
    r'(?:[;|&]\s*(?:cd|rm|del|rd|shutdown|wget|curl|powershell|cmd|bash|sh|python|node|rmdir)\b'
    r'|`|\$\(|\b(?:sudo|eval|exec)\s+[a-z][\w./]*)', re.IGNORECASE)
# 内网地址（SSRF）
SSRF_HOST_RE = re.compile(r'^((127\.0\.0\.1|0\.0\.0\.0|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|localhost|\[::1\]|::1))', re.IGNORECASE)
# 危险文件系统路径
DANGEROUS_PATH_RE = re.compile(r'(/etc/passwd|/etc/shadow|/proc/|C:\\windows\\system32|/boot/|/dev/sd)', re.IGNORECASE)
# 裸危险命令
DANGEROUS_CMD_RE = re.compile(
    r'\b(rm\s+-rf|rm\s+-fr|rmdir\s+/s|del\s+/[fsq]|rd\s+/[fsq]|format\s+[a-z]:'
    r'|shutdown\s+-s|shutdown\s+/s|mkfs\b|fdisk\b|dd\s+if=)', re.IGNORECASE)


class VulnerabilityScanner:
    """漏洞模式扫描器（纯启发式，静态检测）"""

    @staticmethod
    def scan_path_traversal(text: str) -> Optional[str]:
        if PATH_TRAVERSAL_RE.search(text):
            return "路径穿越模式（../）"
        return None

    @staticmethod
    def scan_command_injection(text: str) -> Optional[str]:
        if CMD_INJECTION_RE.search(text):
            return "命令注入模式（shell 元字符 + 危险命令）"
        return None

    @staticmethod
    def scan_ssrf(text: str) -> Optional[str]:
        for part in re.split(r'[\s,\'"]+', text):
            if SSRF_HOST_RE.match(part):
                return f"SSRF 模式（内网地址 {part[:40]}）"
        return None

    @staticmethod
    def scan_dangerous_path(text: str) -> Optional[str]:
        if DANGEROUS_PATH_RE.search(text):
            return "危险系统路径访问"
        return None

    @staticmethod
    def scan_dangerous_command(text: str) -> Optional[str]:
        if DANGEROUS_CMD_RE.search(text):
            return "危险命令"
        return None

    @staticmethod
    def scan_all(text: str) -> List[Dict[str, str]]:
        """全量扫描，返回 [{type, reason}]"""
        found = []
        scanners = [
            ("path_traversal", VulnerabilityScanner.scan_path_traversal),
            ("command_injection", VulnerabilityScanner.scan_command_injection),
            ("ssrf", VulnerabilityScanner.scan_ssrf),
            ("dangerous_path", VulnerabilityScanner.scan_dangerous_path),
            ("dangerous_command", VulnerabilityScanner.scan_dangerous_command),
        ]
        for scan_type, fn in scanners:
            reason = fn(text)
            if reason:
                found.append({"type": scan_type, "reason": reason})
        return found


class SelfCheckMonitor:
    """
    程序异常自检监控器

    用法：
        monitor = SelfCheckMonitor(data_dir="./data", block_unauthorized=True)
        report = monitor.run_startup_checks()   # 启动自检
        blocked, reason = monitor.inspect_tool_call(user_id, tool_name, inputs)  # 运行期
    """

    def __init__(self, data_dir: str = "./data", block_unauthorized: bool = True,
                 scan_depth: int = 2, interval: int = 60):
        self.data_dir = data_dir
        self.block_unauthorized = block_unauthorized
        self.scan_depth = scan_depth
        self.interval = interval
        self.scanner = VulnerabilityScanner()
        self.report: Optional[SelfCheckReport] = None
        self._blocked_events: List[Dict[str, Any]] = []
        self._last_check: float = 0.0

    @property
    def report_file(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, "self_check_report.json")

    # ==================== 启动自检 ====================

    def run_startup_checks(self) -> SelfCheckReport:
        """运行启动自检"""
        checks: List[CheckItem] = []

        checks.append(self._check_dependencies())
        checks.append(self._check_data_dir())
        checks.append(self._check_config_files())
        checks.append(self._check_syspath())
        checks.append(self._check_env())
        checks.append(self._check_tempdir())
        checks.append(self._check_module_shadowing())

        self.report = SelfCheckReport(timestamp=time.time(), checks=checks,
                                      blocked_events=list(self._blocked_events))
        self._save_report()
        return self.report

    def _check_dependencies(self) -> CheckItem:
        deps = ["aiohttp", "aiosqlite", "yaml", "openai"]
        missing = []
        for d in deps:
            try:
                importlib.import_module(d)
            except ImportError:
                missing.append(d)
        if missing:
            return CheckItem("dependencies", False, f"缺少依赖: {missing}", "warn")
        return CheckItem("dependencies", True, "核心依赖可用")

    def _check_data_dir(self) -> CheckItem:
        from pathlib import Path
        from ..utils.paths import safe_join
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            probe = safe_join(self.data_dir, ".self_check_probe")
            Path(probe).write_text("ok", encoding="utf-8")
            os.remove(probe)
            return CheckItem("data_dir", True, f"{self.data_dir} 可写")
        except Exception as e:
            return CheckItem("data_dir", False, f"数据目录不可写: {e}", "critical")

    def _check_config_files(self) -> CheckItem:
        # Windows 权限模型与 Unix 不同（st_mode 权限位不可靠），仅 POSIX 检查
        if sys.platform.startswith("win"):
            return CheckItem("config_files", True, "配置存在（Windows 权限模型跳过）")
        problems = []
        for cfg in ["elysia.json", ".env.example"]:
            if os.path.exists(cfg):
                try:
                    mode = os.stat(cfg).st_mode
                    if mode & 0o002:  # world-writable
                        problems.append(f"{cfg} 世界可写，存在被篡改风险")
                except Exception:
                    pass
        if problems:
            return CheckItem("config_files", False, "; ".join(problems), "warn")
        return CheckItem("config_files", True, "配置文件权限正常")

    def _check_syspath(self) -> CheckItem:
        """sys.path 污染检测：扫描可写目录中是否有可疑的 .py 模块"""
        if sys.platform.startswith("win"):
            return CheckItem("syspath", True, "sys.path 正常（Windows 跳过权限位检查）")
        suspicious = []
        for path in list(sys.path)[: self.scan_depth]:
            if not path or not os.path.isdir(path):
                continue
            try:
                mode = os.stat(path).st_mode
                if mode & 0o002:
                    suspicious.append(f"{path}（可写）")
            except Exception:
                pass
        if suspicious:
            return CheckItem("syspath", False, f"检测到可写目录在 sys.path 中: {suspicious}", "warn")
        return CheckItem("syspath", True, "sys.path 正常")

    def _check_env(self) -> CheckItem:
        issues = []
        if os.getenv("PYTHONINSPECT"):
            issues.append("PYTHONINSPECT 已设置")
        if os.getenv("PYTHONSTARTUP") and os.path.exists(os.getenv("PYTHONSTARTUP", "")):
            issues.append("PYTHONSTARTUP 指向外部文件")
        if issues:
            return CheckItem("env", False, "; ".join(issues), "warn")
        return CheckItem("env", True, "环境变量正常")

    def _check_tempdir(self) -> CheckItem:
        import tempfile
        from pathlib import Path
        tmp = tempfile.gettempdir()
        try:
            probe = os.path.join(tmp, f".elysia_probe_{os.getpid()}")
            Path(probe).write_text("ok", encoding="utf-8")
            os.remove(probe)
            return CheckItem("tempdir", True, f"临时目录可写且正常: {tmp}")
        except Exception as e:
            return CheckItem("tempdir", False, f"临时目录异常: {e}", "warn")

    def _check_module_shadowing(self) -> CheckItem:
        suspicious = []
        shadow_names = {"requests.py", "aiohttp.py", "yaml.py", "json.py",
                        "os.py", "sys.py", "logging.py", "urllib.py", "pathlib.py"}
        for path in list(sys.path)[: self.scan_depth]:
            if not path or not os.path.isdir(path):
                continue
            try:
                for name in os.listdir(path):
                    if name in shadow_names:
                        suspicious.append(os.path.join(path, name))
            except Exception:
                pass
        if suspicious:
            return CheckItem("module_shadowing", False, f"检测到可疑模块覆盖: {suspicious}", "critical")
        return CheckItem("module_shadowing", True, "无模块覆盖")

    # ==================== 运行期监控 ====================

    def inspect_tool_call(self, user_id: str, tool_name: str,
                          inputs: Dict[str, Any]) -> Tuple[bool, str]:
        """
        工具调用前检查（反未知漏洞利用/未授权行为）

        Returns:
            (blocked, reason)  blocked=True 时 runner 应拦截
        """
        if not self.block_unauthorized:
            return False, ""

        text_parts = []
        for k, v in (inputs or {}).items():
            if isinstance(v, str):
                text_parts.append(v)
            elif isinstance(v, (list, dict)):
                try:
                    text_parts.append(json.dumps(v, ensure_ascii=False))
                except Exception:
                    pass

        combined = " ".join(text_parts)
        findings = self.scanner.scan_all(combined)

        if findings:
            event = {
                "at": time.time(), "user_id": user_id, "tool": tool_name,
                "findings": findings, "inputs": {k: str(v)[:200] for k, v in (inputs or {}).items()},
            }
            self._blocked_events.append(event)
            self._save_report()
            reasons = "; ".join(f["reason"] for f in findings)
            logger.warning(f"Self-check blocked tool call {tool_name} by {user_id}: {reasons}")
            return True, f"【安全拦截】{reasons}"

        return False, ""

    def record_auth_violation(self, user_id: str, tool_name: str, reason: str) -> None:
        """记录权限违规（由 AuthManager 调用）"""
        self._blocked_events.append({
            "at": time.time(), "user_id": user_id, "tool": tool_name,
            "findings": [{"type": "unauthorized", "reason": reason}],
        })
        self._save_report()
        logger.warning(f"Unauthorized access recorded: {user_id} -> {tool_name}: {reason}")

    def _save_report(self) -> None:
        try:
            from pathlib import Path
            os.makedirs(self.data_dir, exist_ok=True)
            data = {
                "timestamp": time.time(),
                "blocked_events": self._blocked_events[-200:],
            }
            Path(self.report_file).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def get_summary(self) -> Dict[str, Any]:
        """自检摘要（CLI/WebUI 展示）"""
        return {
            "enabled": True,
            "block_unauthorized": self.block_unauthorized,
            "blocked_events": len(self._blocked_events),
            "recent_blocked": list(reversed(self._blocked_events[-10:])),
            "report_file": self.report_file,
        }