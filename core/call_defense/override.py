"""调用防御豁免状态机 + 防御心跳（阶段四）。

设计语义（与阶段一~三完全独立，不修改任何现有文件）：

1. **豁免 = 临时时间窗**，绝无"永久放行"：
   - OWNER 可通过 `defense off` 发起豁免，进入"豁免模式"
   - 豁免有 **TTL 硬上限**（默认 3600s = 1 小时），到期**自动恢复防御**
   - 豁免期间需 OWNER **心跳续期**；心跳停止超过阈值则防御自动恢复
     （防止"关防御后忘了开"的永久后门）

2. **防御心跳**：独立记录"最近心跳时间"，与守护线程解耦。
   监控端可检查 `is_heartbeat_alive()`——若守护本身宕机/心跳中断会告警。

3. **不持久化**：豁免状态仅存内存，进程重启即失效，不写 data 落盘。

对外接口（供 /call_defense 使用）：
    override_off(operator, ttl=3600) -> Dict
    override_on(operator)            -> Dict   （主动恢复防御）
    heartbeat(operator)              -> Dict   （OWNER 心跳续期）
    override_status()                -> Dict
    is_heartbeat_alive(grace=...)    -> bool
"""
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
import os
import time
import threading

from ..cmd_grade import CommandGrade, grade_from_role
from .audit import log_event, EventSeverity

# 豁免 TTL 硬上限（秒）。阶段四要求：仅豁免一小时。
DEFAULT_OVERRIDE_TTL = 3600          # 1 小时
MAX_OVERRIDE_TTL = 3600              # 无论传入多少，强制封顶 1 小时
HEARTBEAT_GRACE = 90                 # 心跳允许停滞的宽限（秒），超此判定失效
STATE_FILE = "call_defense_override.json"  # 预留（当前不落盘，仅做标记）


class OverrideError(Exception):
    """豁免状态机非法操作。"""


@dataclass
class OverrideState:
    """豁免状态（仅内存，重启失效）。"""
    active: bool = False
    operator: str = ""           # 发起豁免的 OWNER user_id
    started_at: float = 0.0
    expires_at: float = 0.0      # 豁免硬截止（TTL 到期）
    last_heartbeat: float = 0.0  # 最近一次 OWNER 心跳/续期时间
    reason: str = ""


_lock = threading.Lock()
_state = OverrideState()

# 可注入的角色解析器（由接入方注入 AuthManager.get_role，返回 UserRole）。
# 若未注入，则把传入的 operator 视为【角色/档位值】直接换算。
_resolve_role: Any = None


def set_role_resolver(resolver) -> None:
    """注入用户角色解析函数：resolver(user_id) -> 角色值/UserRole。

    供接入方在启动时调用，例如：
        from ..auth import AuthManager
        override.set_role_resolver(auth.get_role)
    注入后，override_off/heartbeat 的 operator 参数即可传 user_id。
    """
    global _resolve_role
    _resolve_role = resolver


def _operator_grade(operator) -> int:
    """把 operator 换算成数值档：优先用注入的解析器当 user_id 解析。"""
    global _resolve_role
    if _resolve_role is not None:
        try:
            role = _resolve_role(operator)
            return grade_from_role(role)
        except Exception:
            pass
    return grade_from_role(operator)


def _role_name(role) -> str:
    g = grade_from_role(role)
    try:
        return CommandGrade(g).name
    except (ValueError, TypeError):
        return str(role)


def _require_owner(operator, action: str) -> None:
    """豁免状态机仅允许 OWNER 操作。

    operator 可传 user_id（若已 set_role_resolver 注入 AuthManager），
    也可直接传角色值（如 CommandGrade.OWNER / UserRole.OWNER）。
    """
    if _operator_grade(operator) < CommandGrade.OWNER:
        raise OverrideError(
            f"{action} 仅允许【所有者(OWNER)】操作，当前档位为【{_role_name(operator)}】。")


def _operator_label(operator) -> str:
    """豁免记录里展示的操作者标识。"""
    return str(operator)


def override_off(operator, ttl: int = DEFAULT_OVERRIDE_TTL,
                 reason: str = "") -> Dict[str, Any]:
    """发起豁免（进入豁免模式）。

    仅 OWNER 可调用。ttl 会被强制封顶到 MAX_OVERRIDE_TTL（1 小时）。
    """
    _require_owner(operator, "开启豁免(defense off)")
    cap = min(int(ttl), MAX_OVERRIDE_TTL) if ttl is not None else DEFAULT_OVERRIDE_TTL
    now = time.time()
    with _lock:
        _state.active = True
        _state.operator = str(operator)
        _state.started_at = now
        _state.expires_at = now + cap
        _state.last_heartbeat = now
        _state.reason = reason or "OWNER 手动豁免"
    log_event(
        EventSeverity.WARN, "override",
        "调用防御已豁免（限时）",
        detail={"operator": str(operator), "ttl_s": cap,
                "expires_at": _state.expires_at,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_state.expires_at))},
    )
    return override_status()


def heartbeat(operator) -> Dict[str, Any]:
    """OWNER 心跳续期（续到 TTL 剩余清零则失效；到期自动恢复）。

    仅豁免模式下、且为原 OWNER 有效；非豁免模式会抛错。
    """
    with _lock:
        if not _state.active:
            raise OverrideError("当前未处于豁免模式，无需心跳。")
        _require_owner(operator, "心跳续期")
        now = time.time()
        # 若已超硬截止，自动恢复，拒绝续期
        if now >= _state.expires_at:
            _state.active = False
            _state.operator = ""
            log_event(EventSeverity.WARN, "override",
                      "豁免到期，防御已自动恢复", detail={"operator": str(operator)})
            raise OverrideError("豁免已到期，防御已自动恢复，请重新发起豁免。")
        _state.last_heartbeat = now
    log_event(EventSeverity.INFO, "override",
              "豁免心跳续期",
              detail={"operator": str(operator),
                      "expires_at": _state.expires_at})
    return override_status()


def override_on(operator) -> Dict[str, Any]:
    """主动恢复防御（关闭豁免）。仅 OWNER 可调用。"""
    _require_owner(operator, "恢复防御(defense on)")
    with _lock:
        was = _state.active
        _state.active = False
        _state.operator = ""
        _state.started_at = 0.0
        _state.expires_at = 0.0
        _state.last_heartbeat = 0.0
        _state.reason = ""
    if was:
        log_event(EventSeverity.INFO, "override",
                  "防御已由 OWNER 主动恢复", detail={"operator": str(operator)})
    return override_status()


def _expire_if_past() -> None:
    """内部：若 TTL 已到期或心跳超宽限，自动恢复防御。"""
    now = time.time()
    with _lock:
        if not _state.active:
            return
        expired = False
        detail: Dict[str, Any] = {}
        if now >= _state.expires_at:
            expired = True
            detail["cause"] = "ttl_expired"
        elif now - _state.last_heartbeat > HEARTBEAT_GRACE:
            expired = True
            detail["cause"] = "heartbeat_stall"
        if expired:
            _state.active = False
            detail["operator"] = _state.operator
            detail["stalled_s"] = round(now - _state.last_heartbeat, 1)
            _state.operator = ""
            log_event(EventSeverity.WARN, "override",
                      "豁免失效，防御已自动恢复", detail=detail)


def override_status() -> Dict[str, Any]:
    """当前豁免 + 心跳状态快照（含 TTL 剩余）。"""
    _expire_if_past()
    with _lock:
        now = time.time()
        if _state.active:
            remaining = max(0.0, _state.expires_at - now)
        else:
            remaining = 0.0
        return {
            "override_active": _state.active,
            "operator": _state.operator,
            "ttl_s": MAX_OVERRIDE_TTL,
            "remaining_s": round(remaining, 1),
            "expires_at": _state.expires_at,
            "expires_time": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_state.expires_at))
                             if _state.active else ""),
            "last_heartbeat": _state.last_heartbeat,
            "heartbeat_alive": (_state.active and
                                (now - _state.last_heartbeat <= HEARTBEAT_GRACE)),
            "heartbeat_grace_s": HEARTBEAT_GRACE,
            "reason": _state.reason,
        }


def is_heartbeat_alive(grace: Optional[float] = None) -> bool:
    """防御守护心跳是否存活（守护进程是否在正常上报）。

    即便不处于豁免模式，守护也应持续心跳；本函数用于监控端判断
    守护进程本身是否宕机/心跳中断。
    """
    _expire_if_past()
    g = grace if grace is not None else HEARTBEAT_GRACE
    if _state.last_heartbeat <= 0:
        return False
    return (time.time() - _state.last_heartbeat) <= g