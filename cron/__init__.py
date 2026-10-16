"""cron - 定时任务模块（阶段二收官）

组件：
- scheduler.CronScheduler: 调度核心（注册/调度/超时/持久化/失败隔离）
- tools.CronManageTool: LLM 工具封装（对话式定时任务管理）
"""
from .scheduler import CronScheduler, CronTask
from .tools import CronManageTool

__all__ = ["CronScheduler", "CronTask", "CronManageTool"]

# 调度器全局实例（Runner 接线后可用）
_default_scheduler: "CronScheduler | None" = None


def get_scheduler() -> "CronScheduler | None":
    """获取全局调度器实例（未接线时为 None）"""
    return _default_scheduler


def set_scheduler(scheduler: "CronScheduler | None") -> None:
    global _default_scheduler
    _default_scheduler = scheduler
