"""模块崩溃恢复（bate_2.4.0）

程序模块化 + 崩溃自愈：
- 模块 = 插件 / 核心组件（LLM、Memory、Guard 等），均视为可管理的单元
- 模块崩溃（异常）时记录：具体模块名、位置（文件:行）、异常类型与信息
- 恢复策略（按严重程度升级）：
  1. reload       ：单模块卸载并重载（插件）
  2. reload_chain ：重载该模块的关联链（依赖它的插件一并重载）
  3. full_reload  ：严重崩溃（连续崩溃 / SystemExit / 核心模块）时完全重载
- 最多自动重载 1 次：超限则标记为禁用并生成崩溃报告，不再循环
- 崩溃报告持久化：data/crash_report.json

用法：
    from elysia_agent.core.recovery import ModuleRecovery
    recovery = ModuleRecovery(plugin_manager=runner.plugin_manager, data_dir="./data")
    result = await recovery.handle_crash(module="plugin:xxx", location=..., exc=...)
"""
import os
import json
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional

from ..utils.logger import get_logger

logger = get_logger("Recovery")

REPORT_FILE = "crash_report.json"
MAX_AUTO_RELOAD = 1  # 最多自动重载次数


@dataclass
class CrashRecord:
    """崩溃记录"""
    module: str
    location: str            # 文件:行
    exc_type: str
    exc_message: str
    trace: str
    timestamp: float
    action: str = ""         # reload / reload_chain / full_reload / give_up
    reload_count: int = 0
    severity: str = "error"  # error / critical


class ModuleRecovery:
    """模块崩溃恢复管理器"""

    def __init__(self, plugin_manager=None, data_dir: str = "./data",
                 max_reload: int = MAX_AUTO_RELOAD):
        self.pm = plugin_manager
        self.data_dir = data_dir
        self.max_reload = max_reload
        self._reload_counts: Dict[str, int] = {}
        self._crashes: List[Dict[str, Any]] = []
        self._disabled: set = set()  # 重载超限后禁用的模块

    # ==================== 报告持久化 ====================

    @property
    def report_file(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, REPORT_FILE)

    def _save(self) -> None:
        try:
            from pathlib import Path
            os.makedirs(self.data_dir, exist_ok=True)
            Path(self.report_file).write_text(
                json.dumps({"crashes": self._crashes[-100:]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ==================== 崩溃处理 ====================

    def record_crash(self, module: str, location: str, exc: BaseException,
                     action: str = "", severity: str = "error") -> CrashRecord:
        """记录崩溃事件"""
        rec = CrashRecord(
            module=module, location=location,
            exc_type=type(exc).__name__, exc_message=str(exc),
            trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:],
            timestamp=time.time(), action=action, severity=severity,
        )
        self._crashes.append(asdict(rec))
        self._save()
        logger.error(f"模块崩溃 [{module}] @ {location}: {type(exc).__name__}: {exc}")
        return rec

    def _dependents(self, module_name: str) -> List[str]:
        """找出依赖该模块的插件（关联链）"""
        if self.pm is None:
            return []
        # plugin:xxx 形式 -> 去掉前缀
        name = module_name.split(":", 1)[-1]
        deps = []
        for info in self.pm._infos.values():
            if name in info.dependencies:
                deps.append(info.name)
        return deps

    async def handle_crash(self, module: str, location: str, exc: BaseException,
                           severity: str = "error") -> Dict[str, Any]:
        """
        处理模块崩溃，自动恢复

        策略（最多自动重载 1 次）：
          第 1 次崩溃 -> reload / reload_chain（重载关联链）
          重载后再崩  -> full_reload（完全重载，升级处理）
          仍崩        -> give_up（持久化禁用，不再自动重载）

        Returns:
            {"action": "reload"|"reload_chain"|"full_reload"|"give_up",
             "module": ..., "location": ..., "message": ...}
        """
        count = self._reload_counts.get(module, 0)

        if count >= self.max_reload:
            # 已自动重载过 max_reload 次：标记禁用，不再循环
            self._disabled.add(module)
            rec = self.record_crash(module, location, exc, action="give_up", severity="critical")
            self._mark_disabled(module)
            logger.error(f"模块 {module} 连续崩溃，已停止自动重载（已禁用）")
            return {"action": "give_up", "module": module, "location": location,
                    "message": f"模块 {module} 连续崩溃，已禁用", "crash": asdict(rec)}

        self._reload_counts[module] = count + 1

        # 严重崩溃（异常类型 / 核心模块 / 重载后仍崩）：完全重载
        severe = severity == "critical" or isinstance(exc, (SystemExit, MemoryError)) or count >= 1
        if severe:
            rec = self.record_crash(module, location, exc, action="full_reload", severity="critical")
            await self._full_reload()
            return {"action": "full_reload", "module": module, "location": location,
                    "message": "严重崩溃，已完全重载（第%d次）" % (count + 1), "crash": asdict(rec)}

        # 普通崩溃：单模块重载 + 关联链
        name = module.split(":", 1)[-1]
        deps = self._dependents(name)
        action = "reload_chain" if deps else "reload"
        rec = self.record_crash(module, location, exc, action=action)

        ok = await self._reload_module(module, include_dependents=deps)
        return {"action": action, "module": module, "location": location,
                "message": f"模块已重载（{'关联链: ' + ', '.join(deps) if deps else '单模块'}, 第{count + 1}次）",
                "reloaded": ok, "crash": asdict(rec)}

    # ==================== 恢复动作 ====================

    async def _reload_module(self, module: str, include_dependents: List[str]) -> bool:
        """卸载并重载模块（插件）及其关联链（仅内存卸载，不持久化禁用）"""
        if self.pm is None:
            return False
        name = module.split(":", 1)[-1]

        # 1. 卸载：模块本身 + 关联链（依赖它的插件）
        targets = list(dict.fromkeys([name] + include_dependents))  # 去重保序
        for t in reversed(targets):
            if t in self.pm._plugins:
                try:
                    await self.pm.unload(t, persist=False)
                except Exception as e:
                    logger.warning(f"卸载 {t} 失败: {e}")
        # 2. 重载：反向顺序（依赖先加载）
        ok = True
        for t in targets:
            try:
                if t in self.pm._persisted["swapped"]:
                    r = await self.pm.restore(t)
                    ok = ok and r
                else:
                    info = next((i for i in self.pm.discover() if i.name == t), None)
                    if info:
                        await self.pm._load_single(info)
                    else:
                        ok = False
            except Exception as e:
                logger.error(f"重载 {t} 失败: {e}")
                ok = False
        return ok

    async def _full_reload(self) -> bool:
        """完全重载：内存卸载全部插件后重新加载（不持久化禁用）"""
        if self.pm is None:
            return False
        try:
            for name in list(self.pm._plugins.keys()):
                try:
                    await self.pm.unload(name, persist=False)
                except Exception as e:
                    logger.warning(f"卸载 {name} 失败: {e}")
            loaded = await self.pm.load_all()
            logger.info(f"完全重载完成: {loaded}")
            return True
        except Exception as e:
            logger.error(f"完全重载失败: {e}")
            return False

    def _mark_disabled(self, module: str) -> None:
        """重载超限：持久化禁用（下次启动不加载）"""
        if self.pm is None:
            return
        name = module.split(":", 1)[-1]
        if name not in self.pm._persisted["disabled"]:
            self.pm._persisted["disabled"].append(name)
            self.pm._save_persisted_state()

    # ==================== 查询 ====================

    def get_crash_report(self, limit: int = 20) -> Dict[str, Any]:
        """崩溃报告（模块/位置/异常）"""
        return {
            "total": len(self._crashes),
            "disabled_modules": sorted(self._disabled),
            "reload_counts": dict(self._reload_counts),
            "recent": list(reversed(self._crashes[-limit:])),
            "report_file": self.report_file,
        }

    def is_module_ok(self, module: str) -> bool:
        return module not in self._disabled