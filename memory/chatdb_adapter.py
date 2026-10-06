"""ChatDB Memory Adapter - 对接原C++ ChatDB服务"""
import json
import asyncio
from typing import Dict, Any, Optional, List
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False

from ..interfaces.memory import IMemoryService
from ..utils.logger import get_logger

logger = get_logger("ChatDBMemory")

class ChatDBMemory(IMemoryService):
    """
    ChatDB 记忆适配器

    通过 TCP JSON-RPC 或 HTTP 协议与原 ChatDB C++ 服务通信
    支持：全文搜索、语义搜索、混合搜索、记忆总结
    """

    def __init__(self, host: str = "localhost", port: int = 17320, 
                 password: str = "", db: int = 0, timeout: int = 10):
        self.host = host
        self.port = port
        self.password = password
        self.db = db
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._use_http = True  # 优先HTTP，回退TCP

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def _rpc_call(self, method: str, params: dict) -> Any:
        """发送JSON-RPC请求"""
        payload = {
            "jsonrpc": "2.0",
            "id": asyncio.get_event_loop().time(),
            "method": method,
            "params": params
        }

        url = f"http://{self.host}:{self.port}/rpc"
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if "error" in data:
                    logger.error(f"ChatDB RPC error: {data['error']}")
                    return None
                return data.get("result")
        except Exception as e:
            logger.error(f"ChatDB connection error: {e}")
            return None

    async def retrieve(
        self, user_id: str, query: str, group_id: Optional[str] = None,
        include_recent: int = 3, include_semantic: int = 3,
        include_profile: bool = True, include_active: bool = False,
        include_emotion: bool = False, include_tool_history: bool = False,
        **filters
    ) -> Dict[str, Any]:
        """检索记忆"""
        result = {}

        # 用户画像
        if include_profile:
            profile = await self._rpc_call("search.fulltext", {
                "query": f"profile:{user_id}",
                "limit": 1
            })
            result["profile"] = profile[0]["content"] if profile else ""

        # 近期对话
        if include_recent:
            recent = await self._rpc_call("search.time", {
                "start": 0,  # 从ChatDB获取最近N条
                "end": 9999999999,
                "group_id": group_id,
                "limit": include_recent
            })
            result["recent"] = recent or []

        # 语义搜索
        if include_semantic:
            semantic = await self._rpc_call("search.semantic", {
                "query": query,
                "group_id": group_id,
                "limit": include_semantic
            })
            result["semantic"] = semantic or []

        # 主动规则
        if include_active:
            active = await self._rpc_call("search.marked", {
                "tags": ["active", "rule"],
                "group_id": group_id,
                "limit": 5
            })
            result["active"] = active or []

        # 工具历史
        if include_tool_history:
            tool_hist = await self._rpc_call("search.fulltext", {
                "query": f"tool_history:{user_id}",
                "limit": 3
            })
            result["tool_history"] = tool_hist or []

        return result

    async def store(
        self, user_id: str, memory_type: str, data: Dict[str, Any],
        group_id: Optional[str] = None, importance: float = 0.5,
        tags: Optional[List[str]] = None
    ) -> None:
        """存储记忆"""
        tags = tags or []
        tags.append(memory_type)

        if memory_type == "passive":
            # 存储完整轨迹
            await self._rpc_call("msg.receive", {
                "group_id": group_id or user_id,
                "qq_id": user_id,
                "nickname": "Elysia",
                "content": json.dumps(data, ensure_ascii=False),
                "msg_type": 1,
                "tags": tags
            })
        elif memory_type == "active":
            # 存储提炼规则
            await self._rpc_call("mem.set_importance", {
                "msg_id": data.get("msg_id", ""),
                "score": importance,
                "reason": data.get("reason", "")
            })
        elif memory_type == "emotion":
            # 情绪记录
            await self._rpc_call("msg.receive", {
                "group_id": group_id or user_id,
                "qq_id": user_id,
                "content": f"[Emotion] {json.dumps(data, ensure_ascii=False)}",
                "tags": ["emotion"] + tags
            })

    async def delete(self, memory_id: str) -> bool:
        result = await self._rpc_call("mem.delete", {"mem_id": memory_id})
        return bool(result)

    async def search(
        self, query: str, search_type: str = "hybrid",
        group_id: Optional[str] = None, limit: int = 10, **filters
    ) -> List[Dict[str, Any]]:
        method_map = {
            "fulltext": "search.fulltext",
            "semantic": "search.semantic",
            "hybrid": "search.hybrid",
            "time": "search.time"
        }
        method = method_map.get(search_type, "search.hybrid")
        params = {"query": query, "limit": limit}
        if group_id:
            params["group_id"] = group_id
        params.update(filters)

        result = await self._rpc_call(method, params)
        return result or []

    async def summarize(self, user_id: str, level: str = "24h") -> Dict[str, Any]:
        result = await self._rpc_call("mem.summarize", {
            "group_id": user_id,
            "level": level
        })
        return result or {}

    async def health_check(self) -> bool:
        try:
            result = await self._rpc_call("sys.health", {})
            return result is not None
        except:
            return False

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
