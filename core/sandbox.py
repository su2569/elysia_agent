"""沙箱系统（bate_2.4.0）—— 在配置加载前加载

设计：
- 沙箱在程序最早期激活（读取任何配置之前），以默认安全策略运行
- 配置读取经沙箱校验：文件大小/编码/危险内容/路径保护
- 运行期提供路径穿越、命令注入、危险系统路径检测（与 self_check 联动）
- 保护 sys.modules 关键模块不被恶意覆盖（反 module shadowing）
- 沙箱为模块级单例，未激活时所有检查透传（向后兼容）

用法（main.py 最顶部）:
    from elysia_agent.core.sandbox import sandbox
    sandbox.activate()
    settings = Settings.from_file("elysia.json")  # 内部经沙箱校验
"""
import os
import sys
import re
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("Sandbox")

# 危险系统路径
DANGEROUS_PATHS = (
    "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/proc/", "/boot/",
    "/dev/sd", "/var/run", "/sys/",
    "C:\\Windows\\system32", "C:\\Windows\\System32", "c:/windows/system32",
    "\\\\", "//",
)
# 路径穿越
TRAVERSAL_RE = re.compile(r"(\.\.\s*[\\/]|\.\.%2f|\.\.%5c)", re.IGNORECASE)
# 命令注入（sudo/eval/exec 后必须跟命令参数，避免误伤工具名列表）
CMD_INJECTION_RE = re.compile(
    r"(?:[;|&]\s*(?:cd|rm|del|rd|shutdown|wget|curl|powershell|cmd|bash|sh|python|node|rmdir)\b"
    r"|`|\$\(|\b(?:sudo|eval|exec)\s+[a-z][\w./]*)", re.IGNORECASE
)
# 裸危险命令（无分隔符的破坏性操作）
DANGEROUS_CMD_RE = re.compile(
    r"\b(rm\s+-rf|rm\s+-fr|rmdir\s+/s|del\s+/[fsq]|rd\s+/[fsq]|format\s+[a-z]:"
    r"|shutdown\s+-s|shutdown\s+/s|mkfs\b|fdisk\b|dd\s+if=)", re.IGNORECASE
)
# 关键模块保护列表
CRITICAL_MODULES = ("os", "sys", "builtins", "subprocess", "shutil", "socket",
                    "logging", "pathlib", "json", "codecs", "importlib")
# 最大配置文件大小
MAX_CONFIG_SIZE = 2 * 1024 * 1024  # 2MB


class Sandbox:
    """沙箱单例"""

    _instance: Optional["Sandbox"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.active = False
        self._snapshot: Dict[str, Any] = {}
        self._violations: list = []

    # ==================== 生命周期 ====================

    def activate(self, log: bool = True) -> bool:
        """激活沙箱（在配置加载前调用）"""
        if self.active:
            return True
        self.active = True
        # 保护关键模块
        self._snapshot = {m: sys.modules.get(m) for m in CRITICAL_MODULES}
        if log:
            logger.info("Sandbox activated (before config load)")
        return True

    def is_active(self) -> bool:
        return self.active

    # ==================== 配置保护 ====================

    def read_config(self, path: str) -> str:
        """安全读取配置文件（经沙箱校验）"""
        if not self.active:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        # 1. 路径校验
        bad = self.check_path(path)
        if bad:
            raise PermissionError(f"沙箱拦截: {path} ({bad})")
        # 2. 大小校验
        try:
            size = os.path.getsize(path)
        except OSError as e:
            raise PermissionError(f"沙箱拦截: 无法访问 {path}: {e}")
        if size > MAX_CONFIG_SIZE:
            raise PermissionError(f"沙箱拦截: 配置文件过大 ({size} bytes)")
        # 3. 读取 + 内容校验
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if "\x00" in content:
            self._violations.append(f"config NUL byte: {path}")
            raise PermissionError(f"沙箱拦截: 配置文件含 NUL 字节 {path}")
        # 4. 危险内容特征（命令注入特征出现在配置中）
        if CMD_INJECTION_RE.search(content):
            self._violations.append(f"config cmd injection pattern: {path}")
            raise PermissionError(f"沙箱拦截: 配置含命令注入特征 {path}")
        return content

    def validate_config_data(self, data: Dict[str, Any], path: str = "") -> bool:
        """校验配置数据中的危险字段（plugins_dir/data_dir 指向系统路径等）"""
        if not self.active:
            return True
        for key in ("plugins_dir", "data_dir", "prompts_dir", "config_file"):
            val = data.get(key)
            if isinstance(val, str):
                bad = self.check_path(val)
                if bad:
                    self._violations.append(f"config field {key}={val!r} ({bad})")
                    logger.warning(f"Sandbox: config field {key} blocked ({bad})")
                    return False
        # 允许的目录检查（确保 data_dir 不是系统目录）
        data_dir = data.get("data_dir")
        if isinstance(data_dir, str):
            normalized = os.path.normpath(data_dir).lower()
            for sys_dir in ("/etc", "/boot", "/proc", "/sys", "c:\\windows", "/usr"):
                if normalized.startswith(sys_dir):
                    self._violations.append(f"data_dir in system dir: {data_dir}")
                    return False
        return True

    # ==================== 运行期检查 ====================

    def check_path(self, path: str) -> Optional[str]:
        """路径安全检查：返回拦截原因或 None"""
        if TRAVERSAL_RE.search(path):
            return "路径穿越模式"
        normalized = path.replace("\\", "/").lower()
        for danger in DANGEROUS_PATHS:
            if danger.lower() in normalized:
                return f"危险路径 {danger}"
        return None

    def check_command(self, command: str) -> Optional[str]:
        """命令注入检测：返回拦截原因或 None"""
        if CMD_INJECTION_RE.search(command):
            return "命令注入模式"
        if DANGEROUS_CMD_RE.search(command):
            return "危险命令"
        return None

    def protect_module(self, name: str, module: Any) -> bool:
        """保护关键模块：防止 sys.modules 被外部替换"""
        if not self.active or name not in CRITICAL_MODULES:
            return True
        original = self._snapshot.get(name)
        if original is not None and sys.modules.get(name) is not original:
            sys.modules[name] = original
            self._violations.append(f"module shadowing blocked: {name}")
            return False
        return True

    # ==================== 查询 ====================

    def get_violations(self, limit: int = 100) -> list:
        return list(reversed(self._violations[-limit:]))

    def get_status(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "violations": len(self._violations),
            "recent_violations": self.get_violations(10),
            "critical_modules_protected": len(CRITICAL_MODULES),
        }


# 模块级单例（main.py 启动最早期 import 并 activate）
sandbox = Sandbox()