"""Tool Registry - 工具注册中心

支持全局默认注册表 + 隔离实例模式。
默认所有类方法操作全局单例，测试时可创建隔离实例。

v2.4.0 增强：
- 工具搜索（AI 按关键词检索工具，避免全量注入导致提示词臃肿）
- 用户命令注册（AI 可搜索并使用用户自定义命令）
- 工具常用度排名（按调用次数，热门工具在候选列表靠前）
"""
from typing import Dict, Any, Optional, List
import time

from ..interfaces.tool import ITool


class ToolRegistry:
    """工具注册中心"""

    _default_instance: Optional["ToolRegistry"] = None
    # 启停状态是否已从磁盘读回（只读一次，避免工具热路径反复读盘）
    _states_loaded: bool = False

    def __init__(self):
        self._tools: Dict[str, ITool] = {}
        # 用户命令：{"命令名": {"description", "content", "source", "usage_count"}}
        self._commands: Dict[str, Dict[str, Any]] = {}
        # 工具调用统计：{"工具名": count}
        self._usage: Dict[str, int] = {}
        # 启停状态：{"工具名或命令名": True=禁用}
        # 由 /api/v1/tools/manage 写入，供 get/list/search 统一过滤
        self._disabled: Dict[str, bool] = {}

    # ---------- 实例方法 ----------

    def register_instance(self, tool: ITool) -> None:
        """向当前实例注册工具"""
        self._tools[tool.name] = tool

    def get_instance(self, name: str) -> Optional[ITool]:
        """从当前实例获取工具（已禁用的视为不存在）"""
        if self._disabled.get(name):
            return None
        return self._tools.get(name)

    def unregister_instance(self, name: str) -> bool:
        """从当前实例注销工具"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def list_instance(self) -> Dict[str, ITool]:
        """列出当前实例的所有工具（不含已禁用的）"""
        return {n: t for n, t in self._tools.items() if not self._disabled.get(n)}

    def clear_instance(self) -> None:
        """清空当前实例"""
        self._tools.clear()
        self._commands.clear()

    def register_command_instance(self, name: str, description: str,
                                  content: str, source: str = "user") -> None:
        """注册用户命令（AI 可搜索/调用）"""
        self._commands[name] = {
            "name": name,
            "description": description,
            "content": content,
            "source": source,
            "usage_count": self._commands.get(name, {}).get("usage_count", 0),
        }

    def get_command_instance(self, name: str) -> Optional[Dict[str, Any]]:
        if self._disabled.get(name):
            return None
        return self._commands.get(name)

    def list_commands_instance(self) -> Dict[str, Dict[str, Any]]:
        return {n: c for n, c in self._commands.items() if not self._disabled.get(n)}

    def search_instance(self, query: str, limit: int = 10,
                        include_commands: bool = True) -> List[Dict[str, Any]]:
        """搜索工具和命令（按常用度排序返回）"""
        q = query.lower()
        results: List[Dict[str, Any]] = []

        for name, tool in self._tools.items():
            if self._disabled.get(name):
                continue
            desc = getattr(tool, "description", "") or ""
            haystack = f"{name} {desc}".lower()
            if q in haystack:
                results.append({
                    "type": "tool",
                    "name": name,
                    "description": desc,
                    "usage_count": self._usage.get(name, 0),
                })

        if include_commands:
            for name, cmd in self._commands.items():
                if self._disabled.get(name):
                    continue
                haystack = f"{name} {cmd.get('description', '')} {cmd.get('content', '')}".lower()
                if q in haystack:
                    results.append({
                        "type": "command",
                        "name": name,
                        "description": cmd.get("description", ""),
                        "usage_count": cmd.get("usage_count", 0),
                    })

        # 常用度排序：调用次数多者靠前
        results.sort(key=lambda r: r["usage_count"], reverse=True)
        return results[:limit]

    def record_usage_instance(self, name: str) -> None:
        """记录工具/命令调用（常用度 +1）"""
        if name in self._tools:
            self._usage[name] = self._usage.get(name, 0) + 1
        elif name in self._commands:
            self._commands[name]["usage_count"] = self._commands[name].get("usage_count", 0) + 1

    def get_usage_stats_instance(self) -> Dict[str, int]:
        """工具调用统计（供常用度排名/WebUI 展示）"""
        stats = dict(self._usage)
        for name, cmd in self._commands.items():
            stats[f"cmd:{name}"] = cmd.get("usage_count", 0)
        return stats

    # ---------- 类方法（操作全局默认实例） ----------

    @classmethod
    def _get_default(cls) -> "ToolRegistry":
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    # 用户命令持久化（跨进程保留）

    @classmethod
    def _commands_path(cls) -> str:
        from ..utils.paths import safe_join
        return safe_join("data", "user_commands.json")

    @classmethod
    def _load_persisted_commands(cls) -> None:
        """启动/搜索前加载持久化的用户命令"""
        import os as _os, json as _json
        path = cls._commands_path()
        if _os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                for name, cmd in data.items():
                    cls._get_default()._commands.setdefault(name, cmd)
            except Exception:
                pass

    @classmethod
    def _save_persisted_commands(cls) -> None:
        import os as _os, json as _json
        from pathlib import Path
        path = cls._commands_path()
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            Path(path).write_text(
                _json.dumps(cls._get_default()._commands, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # 工具/命令启停状态持久化（data/tool_states.json）

    @classmethod
    def _states_path(cls) -> str:
        from ..utils.paths import safe_join
        return safe_join("data", "tool_states.json")

    @classmethod
    def _load_persisted_states(cls, force: bool = False) -> None:
        """读取启停状态到默认实例。文件缺失或损坏时保持全启用。

        默认只读一次（_states_loaded），避免 get/list/search 这类热路径
        每次调用都碰磁盘。force=True 可强制重读。
        """
        import os as _os, json as _json
        if cls._states_loaded and not force:
            return
        cls._states_loaded = True
        path = cls._states_path()
        if not _os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        reg = cls._get_default()
        for name, item in data.items():
            if isinstance(item, dict):
                enabled = bool(item.get("enabled", True))
            else:
                enabled = bool(item)
            if enabled:
                reg._disabled.pop(name, None)
            else:
                reg._disabled[name] = True

    @classmethod
    def _save_persisted_states(cls) -> None:
        """原子写启停状态。写失败不抛异常，只保留内存态。"""
        import os as _os, json as _json, tempfile as _tf
        path = cls._states_path()
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            reg = cls._get_default()
            payload = {n: {"enabled": False} for n in reg._disabled}
            fd, tmp = _tf.mkstemp(prefix=".states_", suffix=".tmp", dir=_os.path.dirname(path))
            try:
                with _os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    _json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    _os.fsync(f.fileno())
                _os.replace(tmp, path)
            except Exception:
                try:
                    _os.unlink(tmp)
                except Exception:
                    pass
                raise
        except Exception:
            pass

    @classmethod
    def set_enabled(cls, name: str, enabled: bool) -> None:
        """设置工具/命令启停状态并落盘"""
        # 先读回已有状态，否则本次写盘会把磁盘上其它禁用项一起抹掉
        cls._load_persisted_states()
        reg = cls._get_default()
        if enabled:
            reg._disabled.pop(name, None)
        else:
            reg._disabled[name] = True
        cls._save_persisted_states()

    @classmethod
    def is_enabled(cls, name: str) -> bool:
        """查询工具/命令是否启用（未知名字默认启用）"""
        cls._load_persisted_states()
        return not cls._get_default()._disabled.get(name)

    @classmethod
    def disabled_names(cls) -> List[str]:
        """当前被禁用的工具/命令名列表"""
        cls._load_persisted_states()
        return list(cls._get_default()._disabled.keys())

    @classmethod
    def register(cls, tool: ITool) -> None:
        """注册工具到全局默认注册表"""
        cls._get_default().register_instance(tool)

    @classmethod
    def get(cls, name: str) -> Optional[ITool]:
        """从全局默认注册表获取工具（已禁用的视为不存在）"""
        cls._load_persisted_states()
        return cls._get_default().get_instance(name)

    @classmethod
    def unregister(cls, name: str) -> bool:
        """从全局默认注册表注销工具"""
        return cls._get_default().unregister_instance(name)

    @classmethod
    def list(cls) -> Dict[str, ITool]:
        """列出全局默认注册表的所有工具（不含已禁用的）"""
        cls._load_persisted_states()
        return cls._get_default().list_instance()

    @classmethod
    def clear(cls) -> None:
        """清空全局默认注册表"""
        cls._get_default().clear_instance()

    @classmethod
    def register_command(cls, name: str, description: str, content: str,
                         source: str = "user") -> None:
        """注册用户命令（AI 可搜索并使用，持久化到 data/user_commands.json）"""
        cls._get_default().register_command_instance(name, description, content, source)
        cls._save_persisted_commands()

    @classmethod
    def get_command(cls, name: str) -> Optional[Dict[str, Any]]:
        cls._load_persisted_commands()
        return cls._get_default().get_command_instance(name)

    @classmethod
    def list_commands(cls) -> Dict[str, Dict[str, Any]]:
        cls._load_persisted_commands()
        return cls._get_default().list_commands_instance()

    @classmethod
    def search(cls, query: str, limit: int = 10,
               include_commands: bool = True) -> List[Dict[str, Any]]:
        """搜索工具 + 用户命令（常用度排序，已禁用的不出现在结果里）"""
        cls._load_persisted_commands()
        cls._load_persisted_states()
        return cls._get_default().search_instance(query, limit, include_commands)

    @classmethod
    def record_usage(cls, name: str) -> None:
        """记录工具/命令调用"""
        cls._get_default().record_usage_instance(name)

    @classmethod
    def get_usage_stats(cls) -> Dict[str, int]:
        return cls._get_default().get_usage_stats_instance()

    @classmethod
    def create_isolated(cls) -> "ToolRegistry":
        """创建一个隔离的注册表实例（用于测试）"""
        return cls()