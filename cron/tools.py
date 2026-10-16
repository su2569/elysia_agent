"""Cron 工具封装 - 将定时任务管理暴露给 LLM（阶段二：cron 模块）

安全设计：
1. 所有操作经弱引用拿到调度器实例，调度器未启动时给出可读错误
2. 注册任务时对 name/payload 做长度与内容消毒（防提示词注入）
3. 删除/禁用操作返回明确的成功/失败语义
"""
import re
from typing import Any, Dict
from ..interfaces.tool import ITool
from ..utils.logger import get_logger

logger = get_logger("CronTool")

# 名称消毒：只允许中英文/数字/下划线/连字符/空格，长度 1-32
_NAME_OK = re.compile(r"^[\w\u4e00-\u9fff\- ]{1,32}$")


def _sanitize_query(query: str) -> str:
    """消毒 payload.query：去除控制字符，截断到 2000 字符"""
    cleaned = "".join(c for c in query if c.isprintable() or c in "\n\t")
    return cleaned[:2000]


class CronManageTool(ITool):
    """cron_task_manage - 定时任务管理工具

    LLM 可通过本工具代替用户注册/查看/启停定时任务。
    """

    def __init__(self, scheduler_ref):
        """scheduler_ref: 弱引用（weakref.ref(CronScheduler 实例)）"""
        self._scheduler_ref = scheduler_ref

    @property
    def name(self) -> str:
        return "cron_task_manage"

    @property
    def description(self) -> str:
        return (
            "管理定时任务：注册、查看、启用、禁用或删除。"
            "action=list 查看全部任务；action=register 注册新任务"
            "（需 name/kind/payload_query，kind 可选 interval（interval_sec 间隔秒数）"
            "/daily（at=HH:MM）/weekly（at=HH:MM + weekdays 星期列表））；"
            "action=enable/disable/delete 按 task_id 操作。"
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "register", "enable", "disable", "delete"],
                    "description": "操作类型",
                },
                "task_id": {
                    "type": "string",
                    "description": "目标任务 ID（enable/disable/delete 必填）",
                },
                "name": {
                    "type": "string",
                    "description": "任务名（register 必填，1-32 字符）",
                },
                "kind": {
                    "type": "string",
                    "enum": ["interval", "daily", "weekly"],
                    "description": "调度类型（register 必填）",
                },
                "payload_query": {
                    "type": "string",
                    "description": "定时触发的提示词内容（register 必填）",
                },
                "interval_sec": {
                    "type": "number",
                    "description": "间隔秒数（kind=interval 必填，30-604800）",
                },
                "at": {
                    "type": "string",
                    "description": "触发时刻 HH:MM（kind=daily/weekly 必填）",
                },
                "weekdays": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                    "description": "星期几列表（kind=weekly 必填）",
                },
            },
            "required": ["action"],
        }

    @property
    def category(self) -> str:
        return "system"

    async def execute(self, action: str = "list", **kwargs) -> str:
        scheduler = self._scheduler_ref() if self._scheduler_ref else None
        if scheduler is None:
            return "【错误】定时任务系统未启动"

        try:
            if action == "list":
                tasks = scheduler.list_tasks()
                if not tasks:
                    return "当前没有定时任务。"
                lines = [f"共 {len(tasks)} 个定时任务："]
                for t in tasks:
                    status = "启用" if t["enabled"] else (
                        f"禁用(连续失败{t['consecutive_failures']}次)" if t["consecutive_failures"] else "禁用"
                    )
                    when = (
                        f"间隔{int(t['interval_sec'])}s" if t["kind"] == "interval"
                        else f"每周{','.join(t['weekdays'])} {t['at']}" if t["kind"] == "weekly"
                        else f"每天 {t['at']}"
                    )
                    lines.append(
                        f"- [{t['task_id']}] {t['name']}（{t['kind']}·{when}）{status}；"
                        f"上次: {t['last_result'] or '从未执行'}"
                    )
                return "\n".join(lines)

            if action == "register":
                name = kwargs.get("name", "")
                kind = kwargs.get("kind", "")
                query = kwargs.get("payload_query", "")
                if not _NAME_OK.match(name or ""):
                    return "【错误】任务名不合法（1-32 字符，仅中英文/数字/下划线/连字符/空格）"
                if not query:
                    return "【错误】payload_query 不能为空（需提供定时触发的提示词内容）"
                reg_kwargs: Dict[str, Any] = {"payload": {"query": _sanitize_query(query)}}
                if kind == "interval":
                    reg_kwargs["interval_sec"] = kwargs.get("interval_sec", 300)
                elif kind in ("daily", "weekly"):
                    reg_kwargs["at"] = kwargs.get("at", "08:00")
                    if kind == "weekly":
                        reg_kwargs["weekdays"] = kwargs.get("weekdays") or ["mon"]
                else:
                    return f"【错误】未知的 kind: {kind}（可选 interval/daily/weekly）"
                task_id = scheduler.register(name=name, kind=kind, **reg_kwargs)
                return f"定时任务注册成功: {name} (ID: {task_id})"

            if action in ("enable", "disable", "delete"):
                task_id = kwargs.get("task_id", "")
                if not task_id:
                    return "【错误】缺少 task_id"
                if action == "delete":
                    ok = scheduler.unregister(task_id)
                    return f"任务 {task_id} 已删除" if ok else f"【错误】任务 {task_id} 不存在"
                ok = scheduler.set_enabled(task_id, action == "enable")
                return f"任务 {task_id} 已{'启用' if action == 'enable' else '禁用'}" if ok else f"【错误】任务 {task_id} 不存在"

            return f"【错误】未知 action: {action}"
        except ValueError as e:
            return f"【错误】{e}"
        except Exception as e:
            logger.error(f"cron_task_manage 执行异常: {e}", exc_info=True)
            return f"【错误】操作失败: {str(e)[:150]}"
