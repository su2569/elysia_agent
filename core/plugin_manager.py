"""Plugin Manager - 插件生命周期管理

设计原则：
- 插件是可选的，核心功能不依赖任何插件
- 通过 PluginManager 统一管理加载/启用/禁用
- 每个插件实现 IPlugin 接口，声明自己的依赖和钩子点

v2.4.0 插件状态机（模仿 AstrBot 插件管理，强化内存控制）：
- LOADED   : 已加载，占用内存，注册工具/钩子/提示词段
- UNLOADED : 完全卸载（on_unload 清理资源），持久化到 disabled 列表，下次启动不加载
- SWAPPED  : 从内存转移至 SWAP（元信息/配置快照到 data/swap_plugins/），
             不占用内存，可随时 restore 恢复；下次启动默认不加载
"""
import os
import sys
import json
import importlib.util
from enum import Enum
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

from ..utils.logger import get_logger

logger = get_logger("PluginManager")


class PluginState(str, Enum):
    """插件运行时状态"""
    LOADED = "loaded"
    UNLOADED = "unloaded"  # 完全卸载（持久化，下次启动不加载）
    SWAPPED = "swapped"    # 已转移至 SWAP（不占内存，可 restore）


@dataclass
class PluginInfo:
    """插件元信息"""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    dependencies: List[str] = field(default_factory=list)
    enabled: bool = True
    config: Dict[str, Any] = field(default_factory=dict)
    plugin_dir: str = ""
    kind: str = "native"  # native / astrbot / yunzai / game（兼容层/游戏插件标识）


class IPlugin(ABC):
    """插件基类"""

    @property
    @abstractmethod
    def info(self) -> PluginInfo:
        """返回插件元信息"""
        pass

    async def on_load(self, manager: "PluginManager") -> None:
        """插件加载时调用（注册工具/钩子/提示词段等）"""
        pass

    async def on_unload(self, manager: "PluginManager") -> None:
        """插件卸载时调用（清理资源）"""
        pass

    async def on_swap(self, manager: "PluginManager") -> None:
        """插件转移至 SWAP 时调用（默认与卸载一致，可覆盖以保存额外状态）"""
        await self.on_unload(manager)

    async def on_restore(self, manager: "PluginManager") -> None:
        """插件从 SWAP 恢复时调用（默认与加载一致，可覆盖以恢复额外状态）"""
        await self.on_load(manager)

    async def on_enable(self) -> None:
        """插件被启用时调用"""
        pass

    async def on_disable(self) -> None:
        """插件被禁用时调用"""
        pass

    def get_config(self, key: str, default=None):
        """读取插件配置"""
        return self.info.config.get(key, default)


# 提示词段优先级（智能提示词管理：预算超限时低优先级先裁剪）
class PromptPriority(int, Enum):
    CRITICAL = 100  # 系统/安全，永不裁剪
    HIGH = 80       # 人格/输出格式
    NORMAL = 50     # 记忆/场景
    LOW = 20        # 插件自定义
    CUSTOM = 10     # 用户自定义


@dataclass
class PromptSegment:
    """提示词段（插件注册的智能提示词片段）"""
    key: str
    content: str
    priority: int = PromptPriority.LOW
    category: str = "plugin"
    source: str = ""  # 来源插件名


class PluginManager:
    """
    插件管理器

    支持：
    - 从目录自动发现并加载插件
    - 依赖检查与拓扑排序
    - 热插拔（启用/禁用）
    - 双态关闭：unload（完全卸载，下次不加载）/ swap（转移 SWAP，可恢复）
    - 状态持久化（data/plugin_state.json）
    - 提示词段/技能注册（智能提示词管理，防臃肿）
    """

    STATE_FILE = "plugin_state.json"
    SWAP_DIR = "swap_plugins"

    def __init__(self, plugins_dir: str = "./plugins", data_dir: str = "./data",
                 prompt_manager: Optional[Any] = None):
        self.plugins_dir = plugins_dir
        self.data_dir = data_dir
        self.prompt_manager = prompt_manager  # 可选：转发提示词段
        self._plugins: Dict[str, IPlugin] = {}
        self._infos: Dict[str, PluginInfo] = {}
        self._states: Dict[str, PluginState] = {}
        self._load_order: List[str] = []
        # 持久化状态：{"disabled": [...], "swapped": [...]}
        self._persisted: Dict[str, List[str]] = {"disabled": [], "swapped": []}
        # 插件注册的提示词段：{key: PromptSegment}
        self.prompt_segments: Dict[str, PromptSegment] = {}
        self._load_persisted_state()

    # ==================== 状态持久化 ====================

    @property
    def state_file(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, self.STATE_FILE)

    def _load_persisted_state(self) -> None:
        """加载持久化的插件状态（disabled/swapped 列表）"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._persisted["disabled"] = list(data.get("disabled", []))
                self._persisted["swapped"] = list(data.get("swapped", []))
        except Exception as e:
            logger.warning(f"Failed to load plugin state: {e}")

    def _save_persisted_state(self) -> None:
        """保存插件状态到磁盘"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self._persisted, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save plugin state: {e}")

    def _swap_dir(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, self.SWAP_DIR)

    def _swap_file(self, name: str) -> str:
        # 防路径逃逸：插件名可来自 manifest（恶意插件可写 ../），仅取 basename 并拒绝 . / ..
        from ..utils.paths import safe_join
        safe = os.path.basename(str(name).replace("\\", "/"))
        if safe in ("", ".", ".."):
            safe = "unnamed"
        return safe_join(self._swap_dir(), f"{safe}.json")

    def _save_swap_snapshot(self, info: PluginInfo) -> None:
        """保存插件 SWAP 快照（元信息 + 配置）"""
        os.makedirs(self._swap_dir(), exist_ok=True)
        snapshot = {
            "name": info.name,
            "version": info.version,
            "description": info.description,
            "author": info.author,
            "dependencies": info.dependencies,
            "config": info.config,
            "plugin_dir": info.plugin_dir,
            "kind": info.kind,
        }
        with open(self._swap_file(info.name), "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    def _load_swap_snapshot(self, name: str) -> Optional[PluginInfo]:
        """读取插件 SWAP 快照"""
        try:
            with open(self._swap_file(name), "r", encoding="utf-8") as f:
                data = json.load(f)
            return PluginInfo(
                name=data.get("name", name),
                version=data.get("version", "0.1.0"),
                description=data.get("description", ""),
                author=data.get("author", ""),
                dependencies=data.get("dependencies", []),
                config=data.get("config", {}),
                plugin_dir=data.get("plugin_dir", ""),
                kind=data.get("kind", "native"),
            )
        except Exception:
            return None

    # ==================== 发现 ====================

    def discover(self) -> List[PluginInfo]:
        """扫描插件目录，返回所有发现的插件信息"""
        discovered = []
        if not os.path.isdir(self.plugins_dir):
            logger.warning(f"Plugins directory not found: {self.plugins_dir}")
            return discovered

        for name in sorted(os.listdir(self.plugins_dir)):
            plugin_path = os.path.join(self.plugins_dir, name)
            if not os.path.isdir(plugin_path):
                continue
            manifest_path = os.path.join(plugin_path, "manifest.json")
            init_path = os.path.join(plugin_path, "__init__.py")

            if os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    info = PluginInfo(
                        name=data.get("name", name),
                        version=data.get("version", "0.1.0"),
                        description=data.get("description", ""),
                        author=data.get("author", ""),
                        dependencies=data.get("dependencies", []),
                        enabled=data.get("enabled", True),
                        config=data.get("config", {}),
                        plugin_dir=plugin_path,
                        kind=data.get("kind", "native"),
                    )
                    discovered.append(info)
                except Exception as e:
                    logger.warning(f"Failed to load manifest for {name}: {e}")
            elif os.path.exists(init_path):
                discovered.append(PluginInfo(name=name, plugin_dir=plugin_path))

        return discovered

    # ==================== 加载 ====================

    async def load_all(self, enabled_only: bool = True) -> List[str]:
        """加载所有插件，返回加载成功的插件名列表

        - enabled_only=True 时：manifest.enabled=False 的跳过
        - 持久化 disabled 列表中的：跳过（上次 unload 的插件下次启动不加载）
        - 持久化 swapped 列表中的：跳过（需显式 restore 恢复）
        """
        disabled_set = set(self._persisted["disabled"])
        swapped_set = set(self._persisted["swapped"])

        discovered = self.discover()
        if enabled_only:
            discovered = [d for d in discovered if d.enabled]
        # 跳过已卸载/已 SWAP 的插件
        discovered = [d for d in discovered
                      if d.name not in disabled_set and d.name not in swapped_set]

        order = self._topological_sort(discovered)
        loaded = []

        for info in order:
            try:
                await self._load_single(info)
                loaded.append(info.name)
            except Exception as e:
                logger.error(f"Failed to load plugin {info.name}: {e}", exc_info=True)

        self._load_order = loaded
        logger.info(f"Loaded {len(loaded)} plugins: {loaded}")
        return loaded

    async def _load_single(self, info: PluginInfo) -> IPlugin:
        """加载单个插件"""
        # 临时添加插件目录到 sys.path，加载后移除，避免永久污染
        path_inserted = False
        if info.plugin_dir and info.plugin_dir not in sys.path:
            sys.path.insert(0, info.plugin_dir)
            path_inserted = True

        try:
            init_file = os.path.join(info.plugin_dir, "__init__.py") if info.plugin_dir else ""
            if not init_file or not os.path.exists(init_file):
                raise ImportError(f"No __init__.py in {info.plugin_dir}")

            module_name = f"plugin_{info.name}"
            spec = importlib.util.spec_from_file_location(module_name, init_file)
            if not spec or not spec.loader:
                raise ImportError(f"Cannot load module from {info.plugin_dir}")

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            plugin_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and issubclass(attr, IPlugin)
                        and attr is not IPlugin):
                    plugin_cls = attr
                    break

            if not plugin_cls:
                raise ImportError(f"No IPlugin subclass found in {info.name}")

            plugin = plugin_cls()
            plugin.info.config.update(info.config)
            plugin.info.plugin_dir = info.plugin_dir
            # 同步 manifest 元信息（依赖/版本/类型），关联链重载依赖这些字段
            plugin.info.dependencies = list(info.dependencies)
            plugin.info.version = info.version or plugin.info.version
            plugin.info.kind = info.kind or plugin.info.kind

            await plugin.on_load(self)
            self._plugins[info.name] = plugin
            self._infos[info.name] = plugin.info
            self._states[info.name] = PluginState.LOADED
            return plugin
        finally:
            if path_inserted and info.plugin_dir in sys.path:
                sys.path.remove(info.plugin_dir)

    def _topological_sort(self, infos: List[PluginInfo]) -> List[PluginInfo]:
        """按依赖关系拓扑排序（带循环依赖检测）"""
        name_to_info = {i.name: i for i in infos}
        visited = set()
        result = []
        visiting = set()  # 当前递归栈，用于检测循环依赖

        def visit(info: PluginInfo):
            if info.name in visited:
                return
            if info.name in visiting:
                raise ValueError(f"Plugin dependency cycle detected involving '{info.name}'")
            visiting.add(info.name)
            for dep in info.dependencies:
                if dep in name_to_info:
                    visit(name_to_info[dep])
            visiting.remove(info.name)
            visited.add(info.name)
            result.append(info)

        for info in infos:
            visit(info)
        return result

    # ==================== 查询 ====================

    def get(self, name: str) -> Optional[IPlugin]:
        return self._plugins.get(name)

    def list_plugins(self) -> List[PluginInfo]:
        return list(self._infos.values())

    def plugin_state(self, name: str) -> PluginState:
        """查询插件运行时状态"""
        return self._states.get(name, PluginState.UNLOADED)

    def get_state_summary(self) -> List[Dict[str, Any]]:
        """返回全部插件状态摘要（用于 CLI/WebUI 展示）"""
        by_name: Dict[str, PluginInfo] = {i.name: i for i in self.discover()}
        summary = []
        names = set(by_name.keys()) | set(self._persisted["disabled"]) | set(self._persisted["swapped"])
        for name in sorted(names):
            info = by_name.get(name) or self._infos.get(name)
            if name in self._persisted["disabled"]:
                state = PluginState.UNLOADED
            elif name in self._persisted["swapped"]:
                state = PluginState.SWAPPED
            else:
                state = self.plugin_state(name)
            summary.append({
                "name": name,
                "state": state.value,
                "loaded": name in self._plugins,
                "enabled": bool(info and info.enabled),
                "version": info.version if info else "",
                "description": info.description if info else "",
                "kind": info.kind if info else "native",
            })
        return summary

    # ==================== 启用/禁用 ====================

    async def enable(self, name: str) -> bool:
        """启用插件：从持久化 disabled 列表移除（下次启动会加载）"""
        if name in self._persisted["disabled"]:
            self._persisted["disabled"].remove(name)
            self._save_persisted_state()
        plugin = self._plugins.get(name)
        if plugin:
            plugin.info.enabled = True
            await plugin.on_enable()
            return True
        # 插件未加载但已解除 disabled/swapped（下次 load_all 会加载）
        return name not in self._persisted["disabled"] and name not in self._persisted["swapped"]

    async def disable(self, name: str) -> bool:
        """禁用插件：内存中停用（不持久化，下次启动仍会加载）"""
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        plugin.info.enabled = False
        await plugin.on_disable()
        return True

    # ==================== 双态关闭 ====================

    async def unload(self, name: str, persist: bool = True) -> bool:
        """完全卸载：从内存移除

        Args:
            persist: True=持久化到 disabled（下次启动不加载）；
                     False=仅内存卸载（供自动重载/恢复流程使用）
        """
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return False
        try:
            await plugin.on_unload(self)
        except Exception as e:
            logger.warning(f"on_unload error for {name}: {e}")
        self._infos.pop(name, None)
        self._states[name] = PluginState.UNLOADED
        self._unregister_prompt_segments(name)
        if persist:
            # 持久化：下次启动不加载
            if name not in self._persisted["disabled"]:
                self._persisted["disabled"].append(name)
            if name in self._persisted["swapped"]:
                self._persisted["swapped"].remove(name)
            self._save_persisted_state()
            logger.info(f"Plugin {name} unloaded (will not load on next start)")
        else:
            logger.info(f"Plugin {name} unloaded (memory only)")
        return True

    async def swap(self, name: str) -> bool:
        """转移 SWAP：从内存移除（不占内存），快照保存，可随时 restore"""
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return False
        try:
            await plugin.on_swap(self)
        except Exception as e:
            logger.warning(f"on_swap error for {name}: {e}")
        info = self._infos.pop(name, None)
        if info is not None:
            self._save_swap_snapshot(info)
        self._states[name] = PluginState.SWAPPED
        self._unregister_prompt_segments(name)
        if name not in self._persisted["swapped"]:
            self._persisted["swapped"].append(name)
        if name in self._persisted["disabled"]:
            self._persisted["disabled"].remove(name)
        self._save_persisted_state()
        logger.info(f"Plugin {name} swapped out (memory released, restore to bring back)")
        return True

    async def restore(self, name: str) -> bool:
        """从 SWAP 恢复插件（重新加载）"""
        if name in self._plugins:
            return True  # 已在内存
        info = self._load_swap_snapshot(name)
        if not info:
            # 回退：尝试从插件目录发现
            for d in self.discover():
                if d.name == name:
                    info = d
                    break
        if not info:
            logger.warning(f"Plugin {name} not found in SWAP or plugins dir")
            return False
        try:
            await self._load_single(info)
            self._persisted["swapped"].remove(name)
            self._save_persisted_state()
            logger.info(f"Plugin {name} restored from SWAP")
            return True
        except Exception as e:
            logger.error(f"Failed to restore plugin {name}: {e}", exc_info=True)
            return False

    async def unload_all(self) -> None:
        for name in list(self._plugins.keys()):
            await self.unload(name)

    # ==================== 提示词段（智能提示词管理） ====================

    def register_prompt_segment(self, key: str, content: str, priority: int = PromptPriority.LOW,
                                category: str = "plugin", source: str = "") -> None:
        """插件注册提示词段（支持优先级，预算超限时低优先级先裁剪）"""
        self.prompt_segments[key] = PromptSegment(
            key=key, content=content, priority=priority, category=category, source=source
        )
        if self.prompt_manager is not None and hasattr(self.prompt_manager, "register_segment"):
            self.prompt_manager.register_segment(
                key=key, content=content,
                layer=60,  # PromptLayer.PLUGIN
                priority=priority, category=category, source=source,
            )
        logger.debug(f"Prompt segment registered: {key} (priority={priority}, source={source})")

    def register_skill(self, key: str, content: str, source: str = "",
                       search_tags: Optional[List[str]] = None) -> None:
        """插件注册技能（技能层：不自动注入，AI 按需搜索/渲染）"""
        self.prompt_segments[key] = PromptSegment(
            key=key, content=content, priority=PromptPriority.HIGH,
            category="skill", source=source
        )
        if self.prompt_manager is not None and hasattr(self.prompt_manager, "register_segment"):
            self.prompt_manager.register_segment(
                key=key, content=content,
                layer=40,  # PromptLayer.SKILL
                priority=80, category="skill", source=source,
                search_tags=list(search_tags or []),
            )
        logger.debug(f"Skill registered: {key} (source={source})")

    def register_user_segment(self, key: str, content: str, priority: int = PromptPriority.HIGH) -> None:
        """注册用户层段（用户自定义规则，紧跟安全防护之前注入）"""
        if self.prompt_manager is not None and hasattr(self.prompt_manager, "register_segment"):
            self.prompt_manager.register_segment(
                key=key, content=content,
                layer=30,  # PromptLayer.USER
                priority=priority, category="user", source="user",
            )

    def search_segments(self, query: str, limit: int = 10):
        """搜索已注册的提示词段/技能（AI 可用工具）"""
        if self.prompt_manager is not None and hasattr(self.prompt_manager, "search_segments"):
            return self.prompt_manager.search_segments(query, limit=limit)
        q = query.lower()
        return [s for s in self.prompt_segments.values()
                if q in (s.key + s.content).lower()][:limit]

    def unregister_prompt_segment(self, key: str) -> None:
        self.prompt_segments.pop(key, None)
        if self.prompt_manager is not None and hasattr(self.prompt_manager, "unregister_segment"):
            self.prompt_manager.unregister_segment(key)

    def _unregister_prompt_segments(self, source: str) -> None:
        """卸载插件时清理其注册的提示词段"""
        for key in [k for k, s in self.prompt_segments.items() if s.source == source]:
            self.unregister_prompt_segment(key)

    def get_prompt_segments(self) -> List[PromptSegment]:
        """返回按优先级排序的提示词段（高优先级在前）"""
        return sorted(self.prompt_segments.values(), key=lambda s: s.priority, reverse=True)

    # ==================== 插件服务 ====================

    def get_llm_provider(self, name: str) -> Optional[Any]:
        plugin = self.get("llm_providers")
        if plugin and hasattr(plugin, "get_provider"):
            return plugin.get_provider(name)
        return None

    def get_memory_service(self, name: str = "vector") -> Optional[Any]:
        plugin = self.get("vector_memory")
        if plugin and hasattr(plugin, "get_service"):
            return plugin.get_service(name)
        return None