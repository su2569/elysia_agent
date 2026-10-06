"""彩色日志系统（跨平台）

- Windows 自动启用 ANSI VT 支持，Linux/macOS 原生支持
- 级别着色：DEBUG 灰 / INFO 绿 / WARNING 黄 / ERROR 红 / CRITICAL 红底白字
- 时间戳灰、logger 名青、消息按级别着色
- 重定向到文件/管道时自动禁用颜色（isatty 检测）
- 文件日志保持纯文本（无 ANSI 码）
"""
import os
import sys
import logging
from typing import Optional

# ==================== ANSI 颜色 ====================

class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    # 前景
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"
    # 背景
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"

# 级别 -> (前景, 背景)
_LEVEL_STYLES = {
    logging.DEBUG: (_Colors.GRAY, ""),
    logging.INFO: (_Colors.GREEN, ""),
    logging.WARNING: (_Colors.YELLOW, ""),
    logging.ERROR: (_Colors.RED, ""),
    logging.CRITICAL: (_Colors.WHITE, _Colors.BG_RED),
}


def _enable_ansi() -> bool:
    """启用 ANSI 颜色支持，返回是否可用"""
    if os.environ.get("ELYSIA_NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True

# 终端是否支持颜色（仅 stdout 为 tty 时）
_COLOR_ENABLED = False


def init_color() -> None:
    """初始化颜色支持（幂等）"""
    global _COLOR_ENABLED
    _COLOR_ENABLED = _enable_ansi() and bool(getattr(sys.stdout, "isatty", lambda: False)())


class _ColorFormatter(logging.Formatter):
    """终端彩色格式器"""

    def __init__(self, fmt: str, datefmt: str = "%H:%M:%S"):
        super().__init__(fmt, datefmt)
        self._plain_fmt = fmt

    def format(self, record: logging.LogRecord) -> str:
        if not _COLOR_ENABLED:
            return super().format(record)
        fg, bg = _LEVEL_STYLES.get(record.levelno, ("", ""))
        # 时间戳灰
        ts = _Colors.GRAY + self.formatTime(record, self.datefmt) + _Colors.RESET
        # 级别着色
        level = fg + bg + f"{record.levelname:<8}" + _Colors.RESET
        # logger 名青色
        name = _Colors.CYAN + record.name + _Colors.RESET
        # 消息按级别着色
        message = fg + record.getMessage() + _Colors.RESET
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return f"{ts} [{name}] {level} {message}"


class _PlainFormatter(logging.Formatter):
    """文件/重定向纯文本格式器"""


def _make_handler(stream=None, log_file: Optional[str] = None) -> logging.Handler:
    handlers = []
    if stream is not None:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_ColorFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        handlers.append(handler)
    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(_PlainFormatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(fh)
    return handlers


def get_logger(name: str = "Elysia") -> logging.Logger:
    """获取命名日志器（终端彩色）

    所有 logger 统一由 root 输出，避免重复打印。
    未调用 setup_logging 时自动配置默认 root（INFO + 彩色终端）。
    """
    logger = logging.getLogger(name)
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_ColorFormatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        root.addHandler(handler)
    return logger


def setup_logging(level: str = "INFO", log_file: Optional[str] = None):
    """配置全局彩色日志

    Args:
        level: DEBUG/INFO/WARNING/ERROR
        log_file: 日志文件路径（纯文本，无 ANSI 码）
    """
    init_color()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 清理旧 handler（避免重复）
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in _make_handler(sys.stdout, log_file):
        root.addHandler(h)


# 启动时初始化颜色
init_color()