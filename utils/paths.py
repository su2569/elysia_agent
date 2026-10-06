"""安全路径工具（v2.5.0，反恶意管理员 / 恶意配置）

任何来自配置或外部输入的路径拼接必须经过此处：

- safe_join：拼接时拒绝绝对路径子部件与路径穿越（..），防止把报告/状态文件
  写到预期之外的位置
- validate_path_fragment：校验单个路径片段（来自配置字段）是否含逃逸

注意：data_dir/plugins_dir 等配置路径自身在配置加载时已由沙箱
（core.sandbox.validate_config_data）校验，此处是纵深防御的第二层。
"""
import os
from typing import Optional

_TRAVERSAL = ".."
_WIN_DRIVE = ":"


def validate_path_fragment(value: str) -> Optional[str]:
    """校验路径片段（来自配置/插件名/文件名）：返回拒绝原因或 None"""
    if not isinstance(value, str):
        return "非字符串路径"
    if not value:
        return None
    parts = value.replace("\\", "/").split("/")
    if any(seg == _TRAVERSAL for seg in parts):
        return "路径穿越（..）"
    if value.startswith(("/", "\\\\")) or (len(value) >= 2 and value[1] == _WIN_DRIVE):
        return "绝对路径（此处仅允许相对路径片段）"
    return None


def safe_join(base: str, *parts: str) -> str:
    """安全拼接路径：子部件拒绝绝对路径与路径穿越

    Args:
        base: 基准目录（配置字段，加载时已由沙箱校验）
        *parts: 要拼接的相对路径片段（文件名/子目录名）

    Raises:
        ValueError: 任一子部件含路径逃逸或为绝对路径
    """
    cleaned: list = []
    for part in parts:
        reason = validate_path_fragment(part)
        if reason:
            raise ValueError(f"非法路径片段 {part!r}: {reason}")
        cleaned.append(part)
    return os.path.normpath(os.path.join(os.fspath(base), *cleaned))