"""权限管理 + 高危行为授权（v2.4.0）

- 角色体系：owner(3) > admin(2) > user(1)
- 工具权限：普通工具 user 可调用；高危工具需 owner/admin 或授权确认
- 授权机制：高危操作挂起为授权请求，管理员审批后获得限时授权（auth_ttl）
- 持久化：角色/授权状态保存到 data/auth.json，重启不丢失
- 与 runner._execute_action 集成：工具调用前统一鉴权
"""
from typing import Dict, Any, Optional, List
from enum import IntEnum
import os
import json
import time
import uuid

from ..utils.logger import get_logger

logger = get_logger("AuthManager")


class UserRole(IntEnum):
    """用户角色（数值越大权限越高）"""
    USER = 1
    ADMIN = 2
    OWNER = 3


class PermissionDenied(Exception):
    """权限不足异常"""

    def __init__(self, message: str, user_id: str = "", tool_name: str = ""):
        super().__init__(message)
        self.user_id = user_id
        self.tool_name = tool_name


class HighRiskAuthorization:
    """高危操作授权请求"""
    def __init__(self, request_id: str, user_id: str, tool_name: str,
                 inputs: Dict[str, Any], created_at: float):
        self.request_id = request_id
        self.user_id = user_id
        self.tool_name = tool_name
        self.inputs = inputs
        self.created_at = created_at
        self.status = "pending"  # pending / approved / denied
        self.approver_id = ""
        self.decided_at = 0.0


class AuthManager:
    """
    权限管理器

    用法：
        auth = AuthManager(data_dir="./data", owner_ids=[...], admin_ids=[...])
        auth.set_role("u1", UserRole.ADMIN)
        ok, reason, need_confirm = auth.check_tool_access("u1", "shell", dangerous=True)
        if need_confirm:
            req_id = await auth.request_authorization("u1", "shell", {"cmd": "ls"})
            # 管理员审批
            await auth.approve_request(req_id, approver_id="admin1")
    """

    STATE_FILE = "auth.json"

    def __init__(self, data_dir: str = "./data",
                 owner_ids: Optional[List[str]] = None,
                 admin_ids: Optional[List[str]] = None,
                 dangerous_tools: Optional[List[str]] = None,
                 require_confirm: bool = True,
                 auth_ttl: int = 300,
                 max_pending: int = 20):
        self.data_dir = data_dir
        self.owner_ids = set(owner_ids or [])
        self.admin_ids = set(admin_ids or [])
        self.dangerous_tools = set(dangerous_tools or [])
        self.require_confirm = require_confirm
        self.auth_ttl = auth_ttl
        self.max_pending = max_pending
        self._roles: Dict[str, int] = {}          # user_id -> role
        self._authorizations: Dict[str, float] = {}  # (user_id:tool) -> 过期时间
        self._pending: Dict[str, HighRiskAuthorization] = {}
        self._history: List[Dict[str, Any]] = []
        self._load()

    # ==================== 持久化 ====================

    @property
    def state_file(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, self.STATE_FILE)

    def _load(self) -> None:
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._roles = {k: int(v) for k, v in data.get("roles", {}).items()}
                self._authorizations = {
                    k: float(v) for k, v in data.get("authorizations", {}).items()
                }
                for p in data.get("pending", []):
                    req = HighRiskAuthorization(
                        request_id=p["request_id"], user_id=p["user_id"],
                        tool_name=p["tool_name"], inputs=p.get("inputs", {}),
                        created_at=p.get("created_at", time.time()),
                    )
                    req.status = p.get("status", "pending")
                    req.approver_id = p.get("approver_id", "")
                    req.decided_at = p.get("decided_at", 0)
                    self._pending[req.request_id] = req
        except Exception as e:
            logger.warning(f"Failed to load auth state: {e}")

    def _save(self) -> None:
        try:
            from pathlib import Path
            os.makedirs(self.data_dir, exist_ok=True)
            data = {
                "roles": self._roles,
                "authorizations": self._authorizations,
                "pending": [
                    {
                        "request_id": r.request_id, "user_id": r.user_id,
                        "tool_name": r.tool_name, "inputs": r.inputs,
                        "created_at": r.created_at, "status": r.status,
                        "approver_id": r.approver_id, "decided_at": r.decided_at,
                    }
                    for r in self._pending.values()
                ],
            }
            Path(self.state_file).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to save auth state: {e}")

    # ==================== 角色管理 ====================

    def get_role(self, user_id: str) -> UserRole:
        """获取用户角色（配置的 owner/admin 优先，其次持久化角色）"""
        if user_id in self.owner_ids:
            return UserRole.OWNER
        if user_id in self.admin_ids:
            return UserRole.ADMIN
        return UserRole(self._roles.get(user_id, UserRole.USER))

    def set_role(self, user_id: str, role: UserRole) -> None:
        """设置用户角色（持久化）"""
        self._roles[user_id] = int(role)
        self._save()

    def role_hierarchy(self, role: UserRole) -> int:
        return int(role)

    def require_role(self, user_id: str, required: UserRole) -> bool:
        """检查用户角色是否达到要求"""
        return self.role_hierarchy(self.get_role(user_id)) >= self.role_hierarchy(required)

    # ==================== 工具权限检查 ====================

    def is_dangerous(self, tool_name: str) -> bool:
        """判断工具是否高危"""
        return tool_name in self.dangerous_tools

    def check_tool_access(self, user_id: str, tool_name: str,
                          is_dangerous: Optional[bool] = None) -> Dict[str, Any]:
        """
        检查用户能否调用工具

        Returns:
            {
                "allowed": bool,
                "reason": str,
                "needs_confirm": bool,   # 高危工具需要授权确认
                "has_authorization": bool,
            }
        """
        if is_dangerous is None:
            is_dangerous = self.is_dangerous(tool_name)

        if not is_dangerous:
            return {"allowed": True, "reason": "", "needs_confirm": False, "has_authorization": False}

        # 高危工具：owner/admin 直接放行
        role = self.get_role(user_id)
        if role in (UserRole.OWNER, UserRole.ADMIN):
            return {"allowed": True, "reason": f"高危工具，{role.name} 放行",
                    "needs_confirm": False, "has_authorization": False}

        # 普通用户：检查是否已有有效授权
        key = f"{user_id}:{tool_name}"
        expires = self._authorizations.get(key, 0)
        if expires > time.time():
            return {"allowed": True, "reason": "已有授权", "needs_confirm": False,
                    "has_authorization": True}

        if self.require_confirm:
            return {"allowed": False, "reason": "高危操作需要授权",
                    "needs_confirm": True, "has_authorization": False}
        return {"allowed": False, "reason": "权限不足（高危工具）",
                "needs_confirm": False, "has_authorization": False}

    # ==================== 授权流程 ====================

    def request_authorization(self, user_id: str, tool_name: str,
                              inputs: Optional[Dict[str, Any]] = None) -> str:
        """发起高危操作授权请求，返回 request_id"""
        # 清理过期 pending
        self._pending = {k: v for k, v in self._pending.items()
                         if v.status == "pending" and time.time() - v.created_at < self.auth_ttl}
        if len(self._pending) >= self.max_pending:
            raise PermissionDenied("挂起的授权请求过多，请稍后再试", user_id, tool_name)
        request_id = uuid.uuid4().hex[:12]
        self._pending[request_id] = HighRiskAuthorization(
            request_id=request_id, user_id=user_id, tool_name=tool_name,
            inputs=dict(inputs or {}), created_at=time.time(),
        )
        self._save()
        logger.warning(f"High-risk authorization requested: {user_id} -> {tool_name} (req={request_id})")
        return request_id

    def approve_request(self, request_id: str, approver_id: str) -> bool:
        """批准授权请求（管理员/所有者）"""
        req = self._pending.get(request_id)
        if not req or req.status != "pending":
            return False
        if not self.require_role(approver_id, UserRole.ADMIN):
            raise PermissionDenied("只有管理员或所有者可以审批授权", approver_id, "approve")
        req.status = "approved"
        req.approver_id = approver_id
        req.decided_at = time.time()
        self._authorizations[f"{req.user_id}:{req.tool_name}"] = time.time() + self.auth_ttl
        self._history.append({
            "request_id": request_id, "user_id": req.user_id, "tool_name": req.tool_name,
            "action": "approved", "by": approver_id, "at": req.decided_at,
        })
        self._save()
        logger.info(f"Authorization approved: {req.user_id} -> {req.tool_name} by {approver_id}")
        return True

    def deny_request(self, request_id: str, approver_id: str) -> bool:
        """拒绝授权请求"""
        req = self._pending.get(request_id)
        if not req or req.status != "pending":
            return False
        if not self.require_role(approver_id, UserRole.ADMIN):
            raise PermissionDenied("只有管理员或所有者可以审批授权", approver_id, "deny")
        req.status = "denied"
        req.approver_id = approver_id
        req.decided_at = time.time()
        self._history.append({
            "request_id": request_id, "user_id": req.user_id, "tool_name": req.tool_name,
            "action": "denied", "by": approver_id, "at": req.decided_at,
        })
        self._save()
        logger.info(f"Authorization denied: {req.user_id} -> {req.tool_name} by {approver_id}")
        return True

    def revoke_authorization(self, user_id: str, tool_name: str) -> None:
        """撤销用户对某工具的授权"""
        self._authorizations.pop(f"{user_id}:{tool_name}", None)
        self._save()

    # ==================== 查询 ====================

    def get_pending_requests(self) -> List[Dict[str, Any]]:
        """列出待审批的授权请求"""
        return [
            {
                "request_id": r.request_id, "user_id": r.user_id,
                "tool_name": r.tool_name, "inputs": r.inputs,
                "created_at": r.created_at,
            }
            for r in self._pending.values() if r.status == "pending"
        ]

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return list(reversed(self._history[-limit:]))

    def get_summary(self) -> Dict[str, Any]:
        """权限状态摘要（CLI/WebUI 展示）"""
        return {
            "roles": {uid: UserRole(r).name for uid, r in self._roles.items()},
            "owners": list(self.owner_ids),
            "admins": list(self.admin_ids),
            "dangerous_tools": sorted(self.dangerous_tools),
            "pending": len(self.get_pending_requests()),
            "active_authorizations": sum(1 for exp in self._authorizations.values() if exp > time.time()),
        }