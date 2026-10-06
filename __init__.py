"""
Elysia Agent Loop bate_2.5.0
========================

纯微内核智能体框架，可嵌入任何项目：
- 双注入记忆引擎（ChatDB + APLR 记忆节点）
- PTD 4.1 提示词防护（Anti-Prompt Injector）
- 多模态感知与子代理调度
- MaiBot 情绪系统
- 插件系统（LLM Providers / MCP / Vector Memory / Agent Graph / Multimodal / Observability / Emotion V2 / Sandbox / Concurrency）
- 并发处理（限流 / 并发控制 / 批量调用 / 并行工具 / 异步记忆队列）
- 产品级平台适配（Telegram / Discord / HTTP Webhook / OneBot v11）+ API 网关（REST + WebSocket）
- 进程保活（ServiceHost 常驻服务，平台崩溃自动重启，绝不结束宿主 Python 进程）

v2.5.0 新增：
- ServiceHost 进程保活：常驻服务管理（平台崩溃自动重启/指数退避/全局异常兜底/优雅关闭）
- 内置命令系统（模仿 AstrBot）：/help /status /stats /memory /clear /reset /guard /version
- 提示词内置回退：pip 安装后无需外部 prompts/ 目录即可工作
- API 网关版本号统一从 version.py 读取

快速开始:
    >>> from elysia_agent import ElysiaRunner, Settings
    >>> from elysia_agent.memory import EnhancedMemory
    >>> runner = ElysiaRunner(llm=..., memory=EnhancedMemory(), settings=Settings())
    >>> result = await runner.run(user_id="user1", query="你好")

常驻服务（不结束 Python 进程）:
    >>> from elysia_agent.lifecycle import ServiceHost
    >>> host = ServiceHost(runner=runner, platforms=[...])
    >>> await host.run_forever()   # 阻塞，Ctrl+C 优雅停止

启用并发模式:
    >>> from elysia_agent.config.settings import ConcurrencyConfig
    >>> settings = Settings()
    >>> settings.concurrency = ConcurrencyConfig(max_concurrent_sessions=50)
    >>> runner = ElysiaRunner(llm=..., memory=..., settings=settings)

嵌入FastAPI示例:
    >>> from fastapi import FastAPI
    >>> app = FastAPI()
    >>> runner = ElysiaRunner(...)
    >>> @app.post("/chat")
    ... async def chat(req: dict):
    ...     return await runner.run(user_id=req["uid"], query=req["msg"])

产品级接入（v2.3.0）:
    >>> from elysia_agent.bot import HTTPWebhookAdapter
    >>> from elysia_agent.api import APIGateway
    >>> gateway = APIGateway(runner=runner, platforms=[HTTPWebhookAdapter({"port": 8080})])
    >>> app = gateway.create_app()  # uvicorn.run(app)
"""

__version__ = "bate_2.5.0"  # 统一版本号在 elysia_agent/version.py（测试版 bate_x.y.z，正式版 1.0.0 清零）
__author__ = "su2569"

# ===== 核心引擎 =====
from .core.runner import ElysiaRunner
from .core.context import AgentContext, Message
from .core.state import LoopState, MessageType, EmotionState, Priority
from .core.plugin_manager import PluginManager, IPlugin, PluginInfo
from .core.concurrency import (
    AtomicCounter, ConcurrentRateLimiter, SessionSemaphore,
    BatchLLMCaller, ParallelToolExecutor, AsyncMemoryQueue, ConcurrencyConfig
)
from .config.settings import Settings
from .config.prompts import PromptManager
from .config.prompt_guard import PromptGuard, PTDEngine, GuardResult
from .decorators import tool, subagent, hook
from .interfaces.llm import ILLMProvider, LLMResponse
from .interfaces.memory import IMemoryService
from .interfaces.tool import ITool
from .interfaces.subagent import ISubAgent
from .interfaces.hook import IHook
from .interfaces.platform import IPlatformAdapter

# ===== 产品级组件（v2.3.0 新增） =====
from .bot import BotMessage, MessageFormat, BasePlatformAdapter, PlatformType
from .bot import TelegramBotAdapter, DiscordBotAdapter, HTTPWebhookAdapter
from .api import APIGateway, MessageRouter

# ===== 进程保活（v2.5.0 新增，不结束 Python 进程） =====
from .lifecycle import ServiceHost, PlatformSupervisor

__all__ = [
    # 核心
    "ElysiaRunner",
    "AgentContext",
    "Message",
    "LoopState",
    "MessageType",
    "EmotionState",
    "Priority",
    "PluginManager",
    "IPlugin",
    "PluginInfo",
    "AtomicCounter",
    "ConcurrentRateLimiter",
    "SessionSemaphore",
    "BatchLLMCaller",
    "ParallelToolExecutor",
    "AsyncMemoryQueue",
    "ConcurrencyConfig",
    "Settings",
    "PromptManager",
    "PromptGuard",
    "PTDEngine",
    "GuardResult",
    "tool",
    "subagent",
    "hook",
    "ILLMProvider",
    "LLMResponse",
    "IMemoryService",
    "ITool",
    "ISubAgent",
    "IHook",
    "IPlatformAdapter",
    # 产品级
    "BotMessage",
    "MessageFormat",
    "BasePlatformAdapter",
    "PlatformType",
    "TelegramBotAdapter",
    "DiscordBotAdapter",
    "HTTPWebhookAdapter",
    "APIGateway",
    "MessageRouter",
    # 进程保活
    "ServiceHost",
    "PlatformSupervisor",
]
