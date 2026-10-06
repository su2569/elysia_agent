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

    def __init__(self):
        self._tools: Dict[str, ITool] = {}
        # 用户命令：{"命令名": {"description", "content", "source", "usage_count"}}
        self._commands: Dict[str, Dict[str, Any]] = {}
        # 工具调用统计：{"工具名": count}
        self._usage: Dict[str, int] = {}

    # ---------- 实例方法 ----------

    def register_instance(self, tool: ITool) -> None:
        """向当前实例注册工具"""
        self._tools[tool.name] = tool

    def get_instance(self, name: str) -> Optional[ITool]:
        """从当前实例获取工具"""
        return self._tools.get(name)

    def unregister_instance(self, name: str) -> bool:
        """从当前实例注销工具"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def list_instance(self) -> Dict[str, ITool]:
        """列出当前实例的所有工具"""
        return dict(self._tools)

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
        return self._commands.get(name)

    def list_commands_instance(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._commands)

    def search_instance(self, query: str, limit: int = 10,
                        include_commands: bool = True) -> List[Dict[str, Any]]:
        """搜索工具和命令（按常用度排序返回）"""
        q = query.lower()
        results: List[Dict[str, Any]] = []

        for name, tool in self._tools.items():
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
        path = cls._commands_path()
        try:
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(cls._get_default()._commands, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @classmethod
    def register(cls, tool: ITool) -> None:
        """注册工具到全局默认注册表"""
        cls._get_default().register_instance(tool)

    @classmethod
    def get(cls, name: str) -> Optional[ITool]:
        """从全局默认注册表获取工具"""
        return cls._get_default().get_instance(name)

    @classmethod
    def unregister(cls, name: str) -> bool:
        """从全局默认注册表注销工具"""
        return cls._get_default().unregister_instance(name)

    @classmethod
    def list(cls) -> Dict[str, ITool]:
        """列出全局默认注册表的所有工具"""
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
        """搜索工具 + 用户命令（常用度排序）"""
        cls._load_persisted_commands()
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