"""FastAPI API 网关 - REST + WebSocket

fastapi 不可用时自动降级为占位实现（包可正常导入），
调用 create_app() 时会给出安装提示。
"""
import asyncio
import json
import time
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from ..core.runner import ElysiaRunner
from ..interfaces.llm import ILLMProvider
from ..interfaces.memory import IMemoryService
from ..config.settings import Settings
from ..version import VERSION as PACKAGE_VERSION
from ..bot.message import BotMessage, MessageFormat
from ..bot.platform_adapter import BasePlatformAdapter
from ..utils.logger import get_logger

logger = get_logger("APIGateway")


if FASTAPI_AVAILABLE:

    class ChatRequest(BaseModel):
        """聊天请求"""
        user_id: str
        query: str = ""
        group_id: Optional[str] = None
        platform: str = "http"
        images: Optional[List[str]] = None

    class ChatResponse(BaseModel):
        """聊天响应"""
        text: str
        success: bool
        error: Optional[str] = None
        session_id: str = ""
        platform: str = "http"

    class HealthResponse(BaseModel):
        """健康检查响应"""
        status: str = "ok"
        version: str = PACKAGE_VERSION
        uptime: float = 0
        active_sessions: int = 0

    class APIGateway:
        """
        FastAPI API 网关

        提供 REST API 和 WebSocket 两种通信方式：

        REST API:
            POST /api/v1/chat       - 聊天
            GET  /api/v1/health     - 健康检查
            GET  /api/v1/stats      - 运行统计
            GET  /api/v1/users      - 用户列表
            DELETE /api/v1/memory/{user_id} - 清除用户记忆

        WebSocket:
            WS /ws/chat - 实时对话（保持会话状态）
        """

        def __init__(
            self,
            runner: ElysiaRunner,
            platforms: Optional[List[BasePlatformAdapter]] = None,
        ):
            self.runner = runner
            self.platforms = platforms or []
            self._start_time = time.time()
            self._active_websockets: Dict[str, WebSocket] = {}

        def create_app(self) -> FastAPI:
            """创建 FastAPI 应用"""
            app = FastAPI(
                title="ElysiaBot API",
                description="ElysiaBot REST + WebSocket API Gateway",
                version=PACKAGE_VERSION,
            )

            # 启动时初始化平台和 Runner
            @app.on_event("startup")
            async def startup():
                await self.runner.start()
                for p in self.platforms:
                    await p.start()

            # 关闭时清理
            @app.on_event("shutdown")
            async def shutdown():
                await self.runner.stop()
                for p in self.platforms:
                    await p.stop()

            # ===== REST API Routes =====

            @app.post("/api/v1/chat", response_model=ChatResponse)
            async def chat(req: ChatRequest):
                """发送聊天消息"""
                try:
                    result = await self.runner.run(
                        user_id=req.user_id,
                        query=req.query,
                        images=req.images,
                        group_id=req.group_id,
                        platform=req.platform,
                    )
                    return ChatResponse(
                        text=result.get("text", ""),
                        success=result.get("success", False),
                        error=result.get("error"),
                        session_id=result.get("session_id", ""),
                        platform=req.platform,
                    )
                except Exception as e:
                    logger.error(f"Chat error: {e}")
                    return ChatResponse(text="", success=False, error=str(e))

            @app.get("/api/v1/health", response_model=HealthResponse)
            async def health():
                """健康检查"""
                return HealthResponse(
                    status="ok",
                    uptime=time.time() - self._start_time,
                    active_sessions=len(self._active_websockets),
                )

            @app.get("/api/v1/stats")
            async def stats():
                """运行统计"""
                return {
                    "version": PACKAGE_VERSION,
                    "uptime": time.time() - self._start_time,
                    "active_websockets": len(self._active_websockets),
                    "platforms": [p.platform_type for p in self.platforms if p.running],
                    "runner_stats": dict(self.runner.stats) if hasattr(self.runner, 'stats') else {},
                }

            @app.get("/api/v1/platforms")
            async def list_platforms():
                """列出所有平台适配器"""
                return {
                    "platforms": [
                        {
                            "type": p.platform_type,
                            "name": p.name,
                            "running": p.running,
                            "supports_multimodal": p.supports_multimodal,
                        }
                        for p in self.platforms
                    ]
                }

            # ===== WebSocket Routes =====

            @app.websocket("/ws/chat")
            async def websocket_chat(ws: WebSocket):
                """WebSocket 实时对话"""
                await ws.accept()
                user_id = None

                try:
                    while True:
                        # 接收消息
                        data = await ws.receive_text()
                        try:
                            msg = json.loads(data)
                        except json.JSONDecodeError:
                            await ws.send_json({"error": "Invalid JSON"})
                            continue

                        # 首次连接时记录 user_id
                        if user_id is None:
                            user_id = msg.get("user_id", str(uuid.uuid4())[:8])
                            await ws.send_json({
                                "type": "connected",
                                "user_id": user_id,
                            })

                        # 调用 Runner
                        result = await self.runner.run(
                            user_id=user_id,
                            query=msg.get("query", ""),
                            images=msg.get("images"),
                            group_id=msg.get("group_id"),
                            platform=msg.get("platform", "websocket"),
                        )

                        # 发送响应
                        await ws.send_json({
                            "type": "response",
                            "text": result.get("text", ""),
                            "success": result.get("success", False),
                            "session_id": result.get("session_id", ""),
                        })

                except WebSocketDisconnect:
                    logger.info(f"WebSocket disconnected: {user_id}")
                except Exception as e:
                    logger.error(f"WebSocket error: {e}")
                finally:
                    if user_id:
                        self._active_websockets.pop(user_id, None)

    class MessageRouter:
        """
        统一消息路由器

        将不同平台的 BotMessage 统一路由到 ElysiaRunner，
        并将 Runner 的结果转换回对应平台格式发送。
        """

        def __init__(self, runner: ElysiaRunner, platforms: List[BasePlatformAdapter]):
            self.runner = runner
            self.platforms = {p.platform_type: p for p in platforms}

        async def handle_message(self, msg: BotMessage) -> None:
            """
            处理来自任何平台的统一消息

            流程：
            1. 过滤/预处理（检查 @、前缀、触发模式）
            2. 调用 ElysiaRunner
            3. 通过对应平台适配器发送回复
            """
            # 调用 Runner
            result = await self.runner.run(
                user_id=msg.user_id,
                query=msg.content,
                images=msg.images if msg.images else None,
                group_id=msg.group_id,
                platform=msg.platform,
            )

            if not result.get("success", False):
                logger.warning(f"Runner failed for {msg.user_id}: {result.get('error')}")
                return

            # 通过对应平台发送回复
            adapter = self.platforms.get(msg.platform_type)
            if adapter:
                target = msg.group_id or msg.user_id
                await adapter.send_message(
                    target_id=target,
                    content=result.get("text", ""),
                    reply_to=msg.message_id,
                )

else:
    # fastapi 不可用时的降级占位（保证 elysia_agent 包可正常导入）

    class ChatRequest:
        """聊天请求（降级实现，无 pydantic 校验）"""
        def __init__(self, **kwargs):
            self.user_id = kwargs.get("user_id", "")
            self.query = kwargs.get("query", "")
            self.group_id = kwargs.get("group_id")
            self.platform = kwargs.get("platform", "http")
            self.images = kwargs.get("images")

    class ChatResponse:
        """聊天响应（降级实现）"""
        def __init__(self, **kwargs):
            self.text = kwargs.get("text", "")
            self.success = kwargs.get("success", False)
            self.error = kwargs.get("error")
            self.session_id = kwargs.get("session_id", "")
            self.platform = kwargs.get("platform", "http")

    class HealthResponse:
        """健康检查响应（降级实现）"""
        def __init__(self, **kwargs):
            self.status = kwargs.get("status", "ok")
            self.version = kwargs.get("version", PACKAGE_VERSION)
            self.uptime = kwargs.get("uptime", 0)
            self.active_sessions = kwargs.get("active_sessions", 0)

    class APIGateway:
        """API 网关（占位）——需要 fastapi 才能使用"""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                "APIGateway 需要 fastapi。请安装: pip install 'elysia-agent[api]' "
                "或 pip install fastapi uvicorn"
            )

        @staticmethod
        def create_app():
            raise ImportError(
                "APIGateway 需要 fastapi。请安装: pip install 'elysia-agent[api]' "
                "或 pip install fastapi uvicorn"
            )

    class MessageRouter:
        """统一消息路由器（占位）——需要 fastapi 才能使用"""

        def __init__(self, *args, **kwargs):
            raise ImportError(
                "MessageRouter 需要 fastapi。请安装: pip install 'elysia-agent[api]' "
                "或 pip install fastapi uvicorn"
            )

        async def handle_message(self, *args, **kwargs):
            raise ImportError(
                "MessageRouter 需要 fastapi。请安装: pip install 'elysia-agent[api]' "
                "或 pip install fastapi uvicorn"
            )