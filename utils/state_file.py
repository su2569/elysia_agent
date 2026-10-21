"""状态文件损坏时的抢救备份（v2.6.1）

背景
----
各模块读取 `data/*.json` 时都做了「解析失败就降级为默认值」的容错，
但**降级之后的下一次保存会原样覆盖磁盘上的损坏原件**：

    plugin_state.json  → 被覆盖成全空 disabled/swapped
    crash_report.json  → 被覆盖成 {"crashes": []}
    cron_state.json    → 被覆盖成全新空任务表

损坏文件里可能还留着不可再生的数据（定时任务、授权记录、用户命令）。
一旦被覆盖，用户没有任何办法找回，也完全不知道发生过什么。

策略
----
解析失败时，先把损坏原件**改名**为 `<原名>.corrupt.<时间戳>`，
再让内存态接管。这样后续覆盖写的是新文件，原件仍可人工抢救。

设计约束
--------
- 本函数绝不抛异常：它处在「读取失败」的兜底路径上，
  如果它自己再抛，会把降级逻辑一起带崩。
- 改名失败（占用/权限）时退化为复制，再失败就只记日志。
- 时间戳用本地时间 `YYYYmmdd_HHMMSS`，便于人工识别。
- 同一秒内重复损坏时追加序号，避免互相覆盖。
"""
import os
import shutil
import time
from typing import Optional


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def backup_corrupt_file(path: str, logger=None, reason: str = "") -> Optional[str]:
    """把解析失败的状态文件改名留档，返回备份路径（失败返回 None）。

    Args:
        path: 损坏的状态文件路径
        logger: 可选 logger，用于记录留档结果
        reason: 可选原因描述，写进日志便于排查

    Returns:
        备份文件的绝对路径；文件不存在或留档失败时为 None。
    """
    try:
        if not path or not os.path.exists(path):
            return None
        if not os.path.isfile(path):
            # 目录/设备等异常占位：不尝试搬运
            return None

        directory = os.path.dirname(path) or "."
        name = os.path.basename(path)
        target = os.path.join(directory, f"{name}.corrupt.{_stamp()}")
        # 同一秒内多次损坏：加序号避免互相覆盖
        if os.path.exists(target):
            for i in range(2, 100):
                candidate = os.path.join(directory, f"{name}.corrupt.{_stamp()}_{i}")
                if not os.path.exists(candidate):
                    target = candidate
                    break

        try:
            os.replace(path, target)
        except Exception:
            # 改名失败（被占用/权限）：退化为复制，原件保留
            try:
                shutil.copy2(path, target)
            except Exception:
                return None

        if logger is not None:
            try:
                suffix = f"（{reason}）" if reason else ""
                logger.warning(
                    f"状态文件解析失败，原件已留档为 {os.path.basename(target)}{suffix}"
                )
            except Exception:
                pass
        return target
    except Exception:
        # 兜底路径上绝不抛异常
        return None
