"""命令执行分级（阶段三）

为命令处理器提供「游客/普通/管理员/所有者」四档分级鉴权。

设计目标：
- 独立新增，不改动现有调用链（CommandHandler 未接入时行为完全不变）
- 与 core/auth.py 的 UserRole 数值档对齐（GUEST=0 < USER=1 < ADMIN=2 < OWNER=3）
- 命令可按所需最低等级声明；低于该档的调用者会被拒绝并返回提示

用法（由 CommandHandler 接入，本模块不直接修改其他文件）：
    from ..core.cmd_grade import CommandGrade, check_command_access

    # 命令声明所需最低等级（默认 USER）
    handler.register("danger", fn, grade=CommandGrade.ADMIN)

    # handle() 内鉴权
    ok, reason = check_command_access(role, required_grade)
    if not ok:
        return {"text": reason, "success": False, "error": "command_grade_denied",
                "is_command": True}
"""
from enum import IntEnum
from typing import Optional, Tuple

from .auth import UserRole

# 与 auth.UserRole 数值档完全对齐（阶段三新增 GUEST=0）
class CommandGrade(IntEnum):
    """命令所需最低权限档（数值越大要求越高）"""
    GUEST = 0
    USER = 1
    ADMIN = 2
    OWNER = 3


def grade_from_role(role) -> int:
    """把 UserRole / CommandGrade / int / 字符串统一换算成数值档"""
    if isinstance(role, (UserRole, CommandGrade)):
        return int(role)
    if isinstance(role, int):
        return role
    if isinstance(role, str):
        try:
            return int(role)
        except (ValueError, TypeError):
            pass
        mapping = {
            "guest": 0, "user": 1, "admin": 2, "owner": 3,
            "游客": 0, "普通": 1, "管理员": 2, "所有者": 3,
        }
        return mapping.get(role.strip().lower(), 1)  # 未知按普通档处理
    return 1


def check_command_access(role, required_grade) -> Tuple[bool, str]:
    """
    检查用户角色是否达到命令所需等级。

    Args:
        role: 用户的当前角色（UserRole 或可换算成数值档的任意形式）
        required_grade: 命令声明的所需等级（CommandGrade 或数值）

    Returns:
        (allowed, reason)  reason 为拒绝原因（allowed=True 时为空串）
    """
    user_grade = grade_from_role(role)
    need_grade = grade_from_role(required_grade)
    if user_grade >= need_grade:
        return True, ""
    need_name = CommandGrade(need_grade).name if need_grade in CommandGrade._value2member_map_ else str(need_grade)
    user_name = CommandGrade(user_grade).name if user_grade in CommandGrade._value2member_map_ else str(user_grade)
    return False, f"权限不足：该命令需要【{need_name}】及以上权限，你的当前档位为【{user_name}】。"


def describe(required_grade) -> str:
    """命令描述尾部追加所需等级（用于 /help 展示）

    未知档位不回落成「普通」：/help 是用户判断能不能跑某条命令的唯一依据，
    把 grade=5 显示成（普通）会让人以为普通账号可用，实际一律被拒。
    """
    value = grade_from_role(required_grade)
    label = {
        0: "游客",
        1: "普通",
        2: "管理员",
        3: "所有者",
    }.get(value)
    if label is None:
        return f"（等级{value}）"
    return f"（{label}）"
