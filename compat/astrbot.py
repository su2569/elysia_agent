"""AstrBot 兼容层（bate_2.4.0，可选）

直接加载 AstrBot 社区插件，提供 astrbot 模块 shim。

支持的插件格式：
1. 新格式（AstrBot v4 装饰器风格）：
   from astrbot.api.event import filter, AstrMessageEvent
   from astrbot.api.star import Context, Star, register
   class MyPlugin(Star):
       def __init__(self, context): super().__init__(context)
       @filter.command("hello", alias={"你好"})
       async def hello(self, event: AstrMessageEvent):
           yield event.plain_result("你好！")
   register(MyPlugin)

2. 旧格式（register() 命令列表）：
   def register():
       return [{"function": fn, "keywords": ["你好"], "description": "..."}]

shim 覆盖：astrbot.api.event（filter/AstrMessageEvent）、astrbot.api.star/all（Star/Context/register）、
astrbot.api.logger、astrbot.core.message.components（Plain/Image）、
astrbot.core.star.filter.command/command_group/permission、astrbot.core.star.star_handler。

启用：elysia.json -> compat.astrbot_enabled: true
"""
import os
import sys
import json
import importlib.util
import inspect
import types
from typing import Dict, Any, List, Optional, Callable

from ..utils.logger import get_logger

logger = get_logger("AstrBotCompat")


# ==================== astrbot 模块 shim ====================

class AstrMessageEvent:
    """astrbot AstrMessageEvent 最小实现（shim）"""
    def __init__(self, user_id: str = "", group_id: str = "",
                 message_str: str = "", self_id: str = "", at_bot: bool = False,
                 message: Optional[list] = None):
        self.user_id = user_id
        self.group_id = group_id
        self.message_str = message_str
        self.self_id = self_id
        self.at_bot = at_bot
        self.message = message or []
        self._replies: List[str] = []

    def reply(self, message: Any) -> None:
        """回复消息（收集到 _replies，由 Elysia 统一发送）"""
        self._replies.append(self._to_text(message))
        return None

    def plain_result(self, message: str) -> "AstrMessageEvent":
        """v4 风格：返回纯文本结果（yield 用，返回 self 供收集）"""
        self._replies.append(str(message))
        return self

    async def send(self, message: Any) -> "AstrMessageEvent":
        self.reply(message)
        return self

    def chain_result(self, messages: Any) -> "AstrMessageEvent":
        """v4 风格：链式结果（图片/文本组合，yield 用）"""
        if isinstance(messages, (list, tuple)):
            for m in messages:
                self._replies.append(self._to_text(m))
        else:
            self._replies.append(self._to_text(messages))
        return self

    def get_message_str(self) -> str:
        return self.message_str

    def is_at_bot(self) -> bool:
        return self.at_bot

    def is_initialized(self) -> bool:
        return True

    @staticmethod
    def _to_text(msg: Any) -> str:
        if isinstance(msg, str):
            return msg
        if isinstance(msg, list):
            return "".join(getattr(m, "text", "") or "" for m in msg)
        return str(msg)


class Plain:
    """astrbot 纯文本消息段"""
    def __init__(self, text: str):
        self.text = str(text)

    def __str__(self):
        return self.text


class Image:
    """astrbot 图片消息段（占位）"""
    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url
        self.type = "image"

    def __str__(self):
        return "[图片]"

    @staticmethod
    def fromBytes(data: bytes) -> "Image":
        """字节图片构造（占位，返回 [图片] 标记）"""
        return Image(file="[bytes]")


class ProviderRequest:
    """astrbot LLM 请求对象（占位）"""
    def __init__(self, *a, **k):
        self.prompt = k.get("prompt", "")
        self.messages = k.get("messages", [])
        self.override_model = None
        self.override_system_prompt = None


# 全局处理器注册表：{handler_id: {type, cmds, alias, permission, fn, cls, star}}
_HANDLERS: List[Dict[str, Any]] = []
# 已注册的 Star 实例：{class_name: instance}
_STAR_INSTANCES: Dict[str, Any] = {}
# 当前正在加载的插件（供 register/装饰器归属使用）
_CURRENT_LOADING_PLUGIN: str = ""
_CURRENT_LOADING_MODULE_PATH: str = ""


class Context:
    """astrbot Context（shim）"""
    def __init__(self):
        self.server = type("Server", (), {"config": {}})()

    def get_all_stars(self) -> List[Any]:
        """返回所有已注册的 Star 实例（help 插件使用）"""
        return list(_STAR_INSTANCES.values())

    def get_star(self, name: str) -> Optional[Any]:
        return _STAR_INSTANCES.get(name)

    def get_registered_star_handlers(self) -> "List[StarHandlerMetadata]":
        """返回已注册处理器元数据（help 插件使用）"""
        metas = []
        for h in _HANDLERS:
            if h.get("type") == "command":
                meta = StarHandlerMetadata(handler_name=", ".join(h.get("cmds", [])))
                meta.handler = h.get("fn")
                meta.star = h.get("star")
                metas.append(meta)
        return metas


class _Filter:
    """filter 对象：命令/事件装饰器"""

    class EventMessageType:
        """事件消息类型（shim）"""
        ALL = "all"
        PRIVATE = "private"
        GROUP = "group"
        FRIEND = "friend"
        GROUP_MSG = "group_msg"
        PRIVATE_MSG = "private_msg"

    def command(self, *cmds, **opts):
        """@filter.command("cmd", alias={"别名"})"""
        def deco(fn):
            cmds_list = list(cmds)
            alias_set = set(opts.get("alias") or opts.get("aliases") or [])
            meta = StarHandlerMetadata(handler_name=fn.__name__, handler_module="")
            meta.handler = fn
            # desc 优先取显式传参，其次从函数 docstring 提取（对齐真实 AstrBot：帮助信息自动从类/方法 docstring 提取）
            desc = opts.get("desc", "")
            if not desc and fn.__doc__:
                desc = (fn.__doc__.strip().split("\n") or [""])[0].strip()
            meta.desc = desc
            cmd_filters = [CommandFilter(c, alias_set) for c in cmds_list]
            if opts.get("permission"):
                cmd_filters.append(PermissionTypeFilter(PermissionType(opts["permission"])))
            meta.event_filters = cmd_filters
            star_handlers_registry.append(meta)
            _HANDLERS.append({
                "type": "command",
                "cmds": cmds_list,
                "alias": alias_set,
                "permission": opts.get("permission"),
                "fn": fn,
                "star": None,
                "meta": meta,
            })
            return fn
        return deco

    def event_message_type(self, *types, **opts):
        """按消息类型过滤（shim：不拦截，原样放行）"""
        def deco(fn):
            return fn
        return deco

    def on_llm_request(self, **opts):
        """LLM 请求拦截（shim：暂不拦截，原样放行）"""
        def deco(fn):
            return fn
        return deco

    def on_decor_result(self, **opts):
        def deco(fn):
            return fn
        return deco

    def on_event(self, event_type, **opts):
        def deco(fn):
            _HANDLERS.append({"type": "event", "event_type": event_type, "fn": fn, "star": None})
            return fn
        return deco


filter = _Filter()


class Star:
    """astrbot Star 基类（shim）"""
    def __init__(self, context: Optional[Context] = None, **kwargs):
        self.context = context or Context()
        self.config = kwargs.get("config", {})
        self._handlers: List[Dict[str, Any]] = []
        # 默认属性（help 插件等读取）
        self.activated = True
        self.name = ""
        self.display_name = ""
        self.star_cls = None
        self.module_path = ""

    def register_handler(self, handler: Dict[str, Any]) -> None:
        self._handlers.append(handler)


class AstrBotConfig:
    """astrbot 配置对象（shim：全部返回默认值）"""
    def __init__(self, *a, **k):
        pass

    def get(self, key, default=None):
        return default
    # 注意：不定义 __getattr__，让 getattr(obj, key, default) 的 default 机制正常工作


def _make_fake_config():
    return AstrBotConfig()


def _parse_register_meta(args: tuple, kwargs: dict) -> Dict[str, str]:
    """解析 @register 的元数据参数。

    兼容真实 AstrBot 两种用法：
    - 弃用式位置参数：@register("name", "author", "desc", "version", "repo")
    - 关键字参数：    @register(name=..., author=..., desc=..., version=..., repo=...)
    - 无参裸装饰：    @register（name 取当前加载插件名或类名）
    """
    meta = {
        "name": kwargs.get("name", ""),
        "author": kwargs.get("author", ""),
        "desc": kwargs.get("desc", ""),
        "version": kwargs.get("version", ""),
        "repo": kwargs.get("repo", ""),
    }
    # 位置参数覆盖（name, author, desc, version, repo）
    pos_names = ["name", "author", "desc", "version", "repo"]
    for i, arg in enumerate(args):
        if i < len(pos_names) and arg:
            meta[pos_names[i]] = str(arg)
    return meta


def register(*args, **kwargs):
    """@register 类装饰器：支持直接修饰类或带参调用（name/version/author/desc）"""
    def _register_cls(cls):
        try:
            meta = _parse_register_meta(args, kwargs)
            try:
                inst = cls(Context())
            except TypeError:
                # 构造签名需要 config 时，提供占位配置
                inst = cls(Context(), config=_make_fake_config())
            _STAR_INSTANCES[cls.__name__] = inst
            # Star 实例属性（help 插件读取）
            inst.activated = True
            # 优先使用 register 元数据 / metadata.yaml 的 name，回退类名
            inst.name = meta["name"] or _CURRENT_LOADING_PLUGIN or cls.__name__
            inst.display_name = getattr(inst, "display_name", "") or ""
            inst.desc = meta["desc"]
            inst.author = meta["author"]
            inst.version = meta["version"]
            inst.repo = meta["repo"]
            inst.star_cls = inst  # 实例满足 isinstance(star_cls, Star) 检查
            # module_path 用于 help 插件按模块匹配 handler
            inst.module_path = _CURRENT_LOADING_MODULE_PATH or f"star:{cls.__name__}"
            # 绑定类方法对应的处理器 + 元数据
            cls_name = cls.__name__
            for h in _HANDLERS:
                qual = getattr(h["fn"], "__qualname__", "")
                if qual.startswith(cls_name + "."):
                    h["star"] = inst
                    if not h.get("plugin"):
                        h["plugin"] = inst.name
                    meta_obj = h.get("meta")
                    if meta_obj is not None:
                        meta_obj.star = inst
                        meta_obj.handler_module_path = inst.module_path
                        meta_obj.desc = meta_obj.desc or (meta["desc"] or "")
            logger.debug(f"astrbot Star registered: {cls.__name__} (name={inst.name})")
        except Exception as e:
            logger.error(f"astrbot Star 初始化失败 {cls.__name__}: {e}")
        return cls

    if len(args) == 1 and callable(args[0]) and not kwargs:
        return _register_cls(args[0])  # @register 直接修饰类
    return _register_cls  # @register(name=..., version=...) 带参数形式


class CommandFilter:
    """命令过滤器（校验消息匹配命令/别名）"""
    def __init__(self, command: str, alias: Optional[set] = None):
        self.command = command
        self.command_name = command[1:] if command.startswith("@") else command
        self.alias = alias or set()

    def check(self, message_str: str) -> bool:
        msg = message_str.strip()
        for c in list(self.alias) + [self.command]:
            if c.startswith("@"):
                c = c[1:]
            if msg == c or msg.startswith(c + " "):
                return True
        return False


class CommandGroupFilter:
    """命令组过滤器（shim）"""
    def __init__(self, name: str, commands: Optional[list] = None):
        self.name = name
        self.group_name = name
        self.commands = commands or []


class PermissionType:
    """权限类型（shim）"""
    ADMIN = "admin"
    GROUP_ADMIN = "group_admin"
    GROUP_MEMBER = "group_member"
    PRIVATE = "private"
    ALL = "all"
    MASTER = "master"
    OWNER = "owner"


class PermissionTypeFilter:
    """权限过滤器（shim）"""
    def __init__(self, permission: PermissionType):
        self.permission = permission
        self.permission_type = permission


class StarHandlerMetadata:
    """处理器元数据（shim）"""
    def __init__(self, handler_name: str = "", event_type: str = "message",
                 event_image: bool = False, handler_module: str = ""):
        self.handler_name = handler_name
        self.event_type = event_type
        self.event_image = event_image
        self.handler_module = handler_module
        self.handler_module_path = handler_module
        self.desc: str = ""
        self.handler: Optional[Callable] = None
        self.star: Optional[Any] = None
        self.event_filters: List[Any] = []


# 全局处理器注册表（star_handler 模块）
star_handlers_registry: List[StarHandlerMetadata] = []


class AstrBotShim:
    """astrbot 模块 shim（供插件 import astrbot 使用）"""
    _installed = False

    @classmethod
    def install(cls) -> None:
        """向 sys.modules 注入最小 astrbot 模块体系"""
        if cls._installed:
            return
        m = types.ModuleType("astrbot")
        m.__path__ = []
        # astrbot.api
        api = types.ModuleType("astrbot.api")
        api.__path__ = []
        api.filter = filter
        api.logger = logger
        api.ProviderRequest = ProviderRequest
        api.AstrBotConfig = AstrBotConfig
        event = types.ModuleType("astrbot.api.event")
        event.filter = filter
        event.AstrMessageEvent = AstrMessageEvent
        star = types.ModuleType("astrbot.api.star")
        star.Context = Context
        star.Star = Star
        star.register = register
        all_mod = types.ModuleType("astrbot.api.all")
        all_mod.Star = Star
        all_mod.register = register
        all_mod.Context = Context
        provider = types.ModuleType("astrbot.api.provider")
        provider.ProviderRequest = ProviderRequest
        api.event = event
        api.star = star
        api.all = all_mod
        api.provider = provider
        m.api = api
        # astrbot.core
        core = types.ModuleType("astrbot.core")
        core.__path__ = []
        message = types.ModuleType("astrbot.core.message")
        message.__path__ = []
        components = types.ModuleType("astrbot.core.message.components")
        components.Plain = Plain
        components.Image = Image
        message.components = components
        core.message = message
        star_mod = types.ModuleType("astrbot.core.star")
        star_mod.__path__ = []
        f_mod = types.ModuleType("astrbot.core.star.filter")
        f_mod.__path__ = []
        cmd = types.ModuleType("astrbot.core.star.filter.command")
        cmd.CommandFilter = CommandFilter
        cg = types.ModuleType("astrbot.core.star.filter.command_group")
        cg.CommandGroupFilter = CommandGroupFilter
        perm = types.ModuleType("astrbot.core.star.filter.permission")
        perm.PermissionType = PermissionType
        perm.PermissionTypeFilter = PermissionTypeFilter
        f_mod.command = cmd
        f_mod.command_group = cg
        f_mod.permission = perm
        star_mod.filter = f_mod
        sh = types.ModuleType("astrbot.core.star.star_handler")
        sh.star_handlers_registry = star_handlers_registry
        sh.StarHandlerMetadata = StarHandlerMetadata
        star_mod.star_handler = sh
        core.star = star_mod
        provider_mod = types.ModuleType("astrbot.core.provider")
        provider_mod.__path__ = []
        entites = types.ModuleType("astrbot.core.provider.entites")
        entites.ProviderRequest = ProviderRequest
        provider_mod.entites = entites
        core.provider = provider_mod
        cfg = types.ModuleType("astrbot.core.config")
        cfg.__path__ = []
        cfg_mod = types.ModuleType("astrbot.core.config.astrbot_config")
        cfg_mod.AstrBotConfig = AstrBotConfig
        cfg.astrbot_config = cfg_mod
        core.config = cfg
        m.core = core
        # 注册到 sys.modules
        sys.modules["astrbot"] = m
        for name, mod in [
            ("astrbot.api", api), ("astrbot.api.event", event), ("astrbot.api.star", star),
            ("astrbot.api.all", all_mod), ("astrbot.api.provider", provider),
            ("astrbot.core", core), ("astrbot.core.message", message),
            ("astrbot.core.message.components", components), ("astrbot.core.star", star_mod),
            ("astrbot.core.star.filter", f_mod), ("astrbot.core.star.filter.command", cmd),
            ("astrbot.core.star.filter.command_group", cg), ("astrbot.core.star.filter.permission", perm),
            ("astrbot.core.star.star_handler", sh), ("astrbot.core.provider", provider_mod),
            ("astrbot.core.provider.entites", entites), ("astrbot.core.config", cfg),
            ("astrbot.core.config.astrbot_config", cfg_mod),
        ]:
            sys.modules[name] = mod
        cls._installed = True
        logger.debug("astrbot shim installed (decorator style)")


# ==================== 兼容适配器 ====================

class AstrBotCompatAdapter:
    """
    AstrBot 插件兼容适配器（装饰器风格 + 旧 register 格式）

    用法：
        adapter = AstrBotCompatAdapter(plugins_dir="./compat/astrbot_plugins")
        count = await adapter.load_all()
        # 命令被转换为 Elysia 用户命令，AI 可搜索调用
        result = await adapter.handle_message(content="帮助", user_id="u1", group_id="g1")
    """

    def __init__(self, plugins_dir: str = "./plugins",
                 include_dirs: Optional[List[str]] = None):
        self.plugins_dir = plugins_dir
        self.include_dirs = include_dirs  # 仅加载这些目录（PluginManager 按 kind 过滤）
        self._commands: Dict[str, Dict[str, Any]] = {}  # 命令名 -> 定义（旧格式）
        self._loaded_plugins: List[str] = []
        self.load_progress: List[Dict[str, Any]] = []   # 加载过程（每插件状态/命令数）
        AstrBotShim.install()

    def discover(self) -> List[str]:
        if not os.path.isdir(self.plugins_dir):
            return []
        # 仅视为插件：目录内含 main.py（排除 ElyBot 内部功能包等非插件目录）
        dirs = [
            d for d in os.listdir(self.plugins_dir)
            if os.path.isdir(os.path.join(self.plugins_dir, d))
            and os.path.exists(os.path.join(self.plugins_dir, d, "main.py"))
        ]
        if self.include_dirs is not None:
            dirs = [d for d in dirs if d in self.include_dirs]
        return sorted(dirs)

    async def load_all(self) -> List[str]:
        """加载所有 AstrBot 插件，返回成功加载的插件名列表（记录加载过程）"""
        loaded = []
        self.load_progress = []
        for name in self.discover():
            try:
                self._load_plugin(name)
                loaded.append(name)
                cmds = sum(1 for c in self.list_commands() if c["plugin"] == name)
                self.load_progress.append({"name": name, "ok": True, "error": "",
                                           "commands": cmds, "kind": "astrbot"})
                logger.info(f"AstrBot 兼容插件已加载: {name}（{cmds} 个命令）")
            except Exception as e:
                self.load_progress.append({"name": name, "ok": False,
                                           "error": str(e)[:120], "commands": 0, "kind": "astrbot"})
                logger.error(f"Failed to load AstrBot plugin {name}: {e}", exc_info=True)
        self._loaded_plugins = loaded
        logger.info(f"AstrBot compat loaded {len(loaded)} plugins: {loaded}")
        return loaded

    def _load_plugin(self, name: str) -> None:
        """加载单个 AstrBot 插件"""
        global _CURRENT_LOADING_PLUGIN, _CURRENT_LOADING_MODULE_PATH
        plugin_dir = os.path.join(self.plugins_dir, name)
        main_path = os.path.join(plugin_dir, "main.py")
        if not os.path.exists(main_path):
            raise FileNotFoundError(f"{main_path} not found")

        sys.path.insert(0, plugin_dir)
        try:
            # 以包内模块加载，支持插件内相对导入（from .xxx import ...）
            pkg_name = f"astrbot_compat_{name}"
            if pkg_name in sys.modules:
                sys.modules.pop(pkg_name)
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [plugin_dir]
            sys.modules[pkg_name] = pkg

            module_name = f"{pkg_name}.main"
            spec = importlib.util.spec_from_file_location(module_name, main_path)
            if not spec or not spec.loader:
                raise ImportError(f"Cannot load {main_path}")
            module = importlib.util.module_from_spec(spec)
            module.__package__ = pkg_name
            sys.modules[module_name] = module
            # 设置当前加载插件上下文，供 register/装饰器归属到本插件
            _CURRENT_LOADING_PLUGIN = name
            _CURRENT_LOADING_MODULE_PATH = module_name
            spec.loader.exec_module(module)

            # 旧格式：register() 返回命令列表（非 list 时跳过）
            if hasattr(module, "register"):
                commands = module.register()
                if not isinstance(commands, list):
                    commands = []
                for cmd in commands:
                    if not isinstance(cmd, dict) or "function" not in cmd:
                        continue
                    func = cmd["function"]
                    for kw in cmd.get("keywords", []):
                        self._commands[kw] = {
                            "function": func, "description": cmd.get("description", f"AstrBot 命令 {name}"),
                            "plugin": name, "is_async": inspect.iscoroutinefunction(func),
                        }
        finally:
            if plugin_dir in sys.path:
                sys.path.remove(plugin_dir)
        logger.debug(f"AstrBot plugin {name} loaded")

    def list_commands(self) -> List[Dict[str, Any]]:
        """列出已注册命令（含装饰器格式）"""
        result = [
            {"keyword": kw, "description": c["description"], "plugin": c["plugin"]}
            for kw, c in self._commands.items()
        ]
        # 装饰器格式的处理器
        for h in _HANDLERS:
            if h.get("type") == "command":
                for c in h.get("cmds", []):
                    result.append({"keyword": c, "description": f"AstrBot 命令 {c}",
                                   "plugin": h.get("plugin") or "star"})
        return result

    async def handle_message(self, content: str, user_id: str = "",
                             group_id: str = "") -> Dict[str, Any]:
        """分发消息到 AstrBot 插件，返回 {handled, replies}"""
        event = AstrMessageEvent(
            user_id=user_id, group_id=group_id, message_str=content,
        )
        # 1. 装饰器格式处理器（优先级高）
        for h in _HANDLERS:
            if h.get("type") != "command":
                continue
            cmds = list(h.get("cmds", [])) + list(h.get("alias", []))
            if not any(content.strip() == c or content.strip().startswith(c + " ")
                       for c in cmds if c):
                continue
            try:
                fn = h["fn"]
                star = h.get("star")
                if star is not None:
                    bound = fn.__get__(star, type(star))
                else:
                    bound = fn
                result = bound(event)
                if inspect.isawaitable(result):
                    result = await result
                # yield 风格（生成器）处理
                if inspect.isasyncgen(result):
                    async for chunk in result:
                        if chunk is not None and hasattr(chunk, "_replies"):
                            event._replies.extend(chunk._replies)
                elif result is not None and hasattr(result, "_replies"):
                    event._replies.extend(result._replies)
            except Exception as e:
                logger.error(f"AstrBot handler error: {e}")
                event._replies.append(f"[AstrBot 插件错误] {str(e)}")
            return {"handled": True, "replies": list(event._replies)}

        # 2. 旧格式命令
        for kw, cmd in self._commands.items():
            if content.strip() == kw or content.strip().startswith(kw + " "):
                try:
                    if cmd["is_async"]:
                        result = cmd["function"](event)
                        if inspect.isawaitable(result):
                            await result
                    else:
                        cmd["function"](event)
                except Exception as e:
                    logger.error(f"AstrBot command {kw} error: {e}")
                    event._replies.append(f"[AstrBot 插件错误] {str(e)}")
                return {"handled": True, "replies": list(event._replies)}

        return {"handled": False, "replies": []}

    def unload_all(self) -> None:
        self._commands.clear()
        _HANDLERS.clear()
        _STAR_INSTANCES.clear()
        self._loaded_plugins = []