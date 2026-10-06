"""AstrBot 兼容层（v2.4.0，可选）

直接加载 AstrBot 社区插件（register() 命令格式），
提供最小 astrbot 模块 shim，将命令处理函数转换为 Elysia 工具。

AstrBot 插件格式（社区标准）：
    # main.py
    def register():
        return [{
            "function": async_command_handler,   # async def fn(message) -> None
            "keywords": ["你好", "hello"],
            "description": "打招呼",
        }]

    async def async_command_handler(message):
        message.reply("你好！我是AstrBot插件")

启用：elysia.yaml -> compat.astrbot_enabled: true
"""
import os
import sys
import importlib.util
import inspect
from typing import Dict, Any, List, Optional, Callable

from ..utils.logger import get_logger

logger = get_logger("AstrBotCompat")


# ==================== astrbot 模块 shim ====================

class MessageEvent:
    """astrbot MessageEvent 最小实现（shim）"""
    def __init__(self, user_id: str = "", group_id: str = "",
                 message_str: str = "", self_id: str = "",
                 unified_msg_origin: str = ""):
        self.user_id = user_id
        self.group_id = group_id
        self.message_str = message_str
        self.self_id = self_id
        self.unified_msg_origin = unified_msg_origin
        self.reply_contents: List[str] = []
        self.sent: bool = False

    def reply(self, message: str) -> None:
        """回复消息（收集到 reply_contents，由 Elysia 统一发送）"""
        self.reply_contents.append(message)
        self.sent = True

    def get_message_str(self) -> str:
        return self.message_str

    def is_initialized(self) -> bool:
        return True


class Context:
    """astrbot Context 最小实现（shim）"""
    def __init__(self):
        self.server = type("Server", (), {"config": {}})()


class AstrBotShim:
    """astrbot 模块 shim（供插件 import astrbot 使用）"""
    _installed = False

    @classmethod
    def install(cls) -> None:
        """向 sys.modules 注入最小 astrbot 模块"""
        if cls._installed:
            return
        module = type(sys)("astrbot")
        module.MessageEvent = MessageEvent
        module.Context = Context
        module.types = type(sys)("astrbot.types")
        module.types.MessageEvent = MessageEvent
        module.types.Context = Context
        module.core = type(sys)("astrbot.core")
        module.core.AstrBot = type("AstrBot", (), {"get_context": lambda self: Context()})
        sys.modules["astrbot"] = module
        sys.modules["astrbot.types"] = module.types
        sys.modules["astrbot.core"] = module.core
        cls._installed = True
        logger.debug("astrbot shim installed")


# ==================== 兼容适配器 ====================

class AstrBotCompatAdapter:
    """
    AstrBot 插件兼容适配器

    用法：
        adapter = AstrBotCompatAdapter(plugins_dir="./compat/astrbot_plugins")
        count = await adapter.load_all()
        # 命令被转换为 Elysia 工具（ToolRegistry），AI 可搜索调用
        result = await adapter.execute_command("你好", user_id="u1", group_id="g1")
    """

    def __init__(self, plugins_dir: str = "./compat/astrbot_plugins"):
        self.plugins_dir = plugins_dir
        self._commands: Dict[str, Dict[str, Any]] = {}  # keyword -> 命令定义
        self._loaded_plugins: List[str] = []
        AstrBotShim.install()

    def discover(self) -> List[str]:
        """发现插件目录"""
        if not os.path.isdir(self.plugins_dir):
            return []
        return sorted([
            d for d in os.listdir(self.plugins_dir)
            if os.path.isdir(os.path.join(self.plugins_dir, d))
        ])

    async def load_all(self) -> List[str]:
        """加载所有 AstrBot 插件，返回成功加载的插件名"""
        loaded = []
        for name in self.discover():
            try:
                self._load_plugin(name)
                loaded.append(name)
            except Exception as e:
                logger.error(f"Failed to load AstrBot plugin {name}: {e}", exc_info=True)
        self._loaded_plugins = loaded
        logger.info(f"AstrBot compat loaded {len(loaded)} plugins: {loaded}")
        return loaded

    def _load_plugin(self, name: str) -> None:
        """加载单个 AstrBot 插件"""
        plugin_dir = os.path.join(self.plugins_dir, name)
        main_path = os.path.join(plugin_dir, "main.py")
        if not os.path.exists(main_path):
            raise FileNotFoundError(f"{main_path} not found")

        sys.path.insert(0, plugin_dir)
        try:
            module_name = f"astrbot_plugin_{name}"
            spec = importlib.util.spec_from_file_location(module_name, main_path)
            if not spec or not spec.loader:
                raise ImportError(f"Cannot load {main_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            if not hasattr(module, "register"):
                raise ImportError(f"AstrBot plugin {name} has no register()")

            commands = module.register()
            for cmd in commands:
                if not isinstance(cmd, dict) or "function" not in cmd:
                    continue
                func = cmd["function"]
                keywords = cmd.get("keywords", [])
                description = cmd.get("description", f"AstrBot 命令 {name}")
                for kw in keywords:
                    self._commands[kw] = {
                        "function": func, "description": description,
                        "plugin": name, "is_async": inspect.iscoroutinefunction(func),
                    }
        finally:
            if plugin_dir in sys.path:
                sys.path.remove(plugin_dir)

        logger.debug(f"AstrBot plugin {name} loaded, {len(self._commands)} commands registered")

    def list_commands(self) -> List[Dict[str, Any]]:
        """列出已注册命令（供 AI 搜索/WebUI 展示）"""
        return [
            {"keyword": kw, "description": c["description"], "plugin": c["plugin"]}
            for kw, c in self._commands.items()
        ]

    def has_command(self, keyword: str) -> bool:
        return keyword in self._commands

    async def execute_command(self, keyword: str, user_id: str = "",
                              group_id: str = "", content: str = "") -> Optional[str]:
        """
        执行 AstrBot 命令

        将消息包装为 shim MessageEvent，执行处理函数，返回 reply 内容。
        """
        cmd = self._commands.get(keyword)
        if not cmd:
            return None
        event = MessageEvent(
            user_id=user_id, group_id=group_id,
            message_str=content or keyword,
        )
        try:
            if cmd["is_async"]:
                result = cmd["function"](event)
                if inspect.isawaitable(result):
                    await result
            else:
                cmd["function"](event)
        except Exception as e:
            logger.error(f"AstrBot command {keyword} error: {e}")
            event.reply(f"【AstrBot 插件错误】{str(e)}")
        return "\n".join(event.reply_contents) if event.reply_contents else None

    def unload_all(self) -> None:
        self._commands.clear()
        self._loaded_plugins = []