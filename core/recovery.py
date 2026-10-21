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
# 最多自动重载次数。必须 >= 2，否则 handle_crash 的三级升级
# （reload -> full_reload -> give_up）无法成立：
# count=0 reload，count=1 full_reload，count>=2 give_up。
# 曾为 1，导致 count>=max_reload 先命中，full_reload 分支永远不可达，
# 第二次崩溃直接持久化禁用模块。
MAX_AUTO_RELOAD = 2

CRASH_LIMIT_DEFAULT = 20
CRASH_LIMIT_MAX = 1000


def _coerce_crash_limit(value) -> int:
    """把外部传入的 limit 夹取到 [1, CRASH_LIMIT_MAX]。

    非数值、None、NaN、inf 一律回落 CRASH_LIMIT_DEFAULT。
    必须显式夹取：0 在切片里是「全量」，负数在切片里是「跳过前 N 条」，
    两者都不是「取最近 N 条」的本意。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return CRASH_LIMIT_DEFAULT
    if n < 1:
        return 1
    if n > CRASH_LIMIT_MAX:
        return CRASH_LIMIT_MAX
    return n


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
        self._last_save_ok: bool = True  # 最近一次 _save 是否成功
        self._load_history()

    # ==================== 报告持久化 ====================

    @property
    def report_file(self) -> str:
        from ..utils.paths import safe_join
        return safe_join(self.data_dir, REPORT_FILE)

    def _load_history(self) -> None:
        """启动时读回崩溃报告

        崩溃报告落盘的唯一意义就是重启后还能看到。只写不读等于没有持久化，
        进程一重启历史就归零。
        """
        try:
            from pathlib import Path
            path = Path(self.report_file)
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            crashes = data.get("crashes", [])
            if isinstance(crashes, list):
                self._crashes = [c for c in crashes if isinstance(c, dict)]
            for c in self._crashes:
                mod = c.get("module", "")
                if mod and c.get("action") == "give_up":
                    self._disabled.add(mod)
        except Exception as e:
            logger.warning(f"读取崩溃报告失败（忽略）: {e}")
            # 损坏原件先留档：崩溃报告是 give_up 禁用状态的唯一载体，
            # 被一次正常保存覆盖后无法重建。
            from ..utils.state_file import backup_corrupt_file
            backup_corrupt_file(self.report_file, logger, reason="崩溃报告解析失败")

    def _save(self) -> bool:
        """把崩溃报告写入磁盘。

        Returns:
            True=已落盘；False=写盘失败（已记 error 日志）。

        原实现 `except Exception: pass` 连日志都不打，失败完全无声。
        崩溃报告是 give_up 禁用状态的唯一持久化载体，写不进去等于
        「本次进程内禁用了，重启后又活过来」，而调用方毫不知情。
        崩溃处理路径本身不能抛异常（否则处理崩溃的代码自己崩），
        所以这里只返回 False + 记日志，由调用方决定如何上报。
        """
        try:
            from pathlib import Path
            os.makedirs(self.data_dir, exist_ok=True)
            target = self.report_file
            tmp = target + ".tmp"
            Path(tmp).write_text(
                json.dumps({"crashes": self._crashes[-100:]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, target)
            self._last_save_ok = True
            return True
        except Exception as e:
            self._last_save_ok = False
            logger.error(f"崩溃报告写入失败（本次记录未持久化，重启后会丢失）: {e}")
            try:
                if os.path.exists(self.report_file + ".tmp"):
                    os.remove(self.report_file + ".tmp")
            except Exception:
                pass
            return False

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
            disabled_persisted = self._mark_disabled(module)
            report_persisted = bool(getattr(self, "_last_save_ok", False))
            all_persisted = disabled_persisted and report_persisted
            if not all_persisted:
                # 本次进程内确实禁用了，但磁盘上没留下痕迹：重启后会复活。
                # 不许沉默——把两个持久化结果如实带出去。
                logger.error(
                    f"模块 {module} 已在本进程内禁用，但持久化失败"
                    f"（禁用状态={disabled_persisted}, 崩溃报告={report_persisted}），"
                    f"重启后会被重新加载"
                )
            logger.error(f"模块 {module} 连续崩溃，已停止自动重载（已禁用）")
            if all_persisted:
                msg = f"模块 {module} 连续崩溃，已禁用"
            else:
                msg = (f"模块 {module} 连续崩溃，已在本进程内禁用，"
                       f"但状态未能写入磁盘，重启后会被重新加载")
            return {"action": "give_up", "module": module, "location": location,
                    "message": msg,
                    "persisted": all_persisted,
                    "disabled_persisted": disabled_persisted,
                    "report_persisted": report_persisted,
                    "crash": asdict(rec)}

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

    def _mark_disabled(self, module: str) -> bool:
        """重载超限：持久化禁用（下次启动不加载）

        Returns:
            True=禁用状态已落盘；False=落盘失败（重启后该模块会被重新加载）。
            一个只在本次进程内生效的「已禁用」，重启后就复活，
            而用户以为它已经被永久停掉了，所以必须如实上报。
        """
        if self.pm is None:
            return False
        name = module.split(":", 1)[-1]
        if name in self.pm._persisted["disabled"]:
            return True
        self.pm._persisted["disabled"].append(name)
        saver = getattr(self.pm, "_save_persisted_state", None)
        if saver is None:
            return False
        try:
            ok = saver()
        except Exception as e:
            logger.error(f"持久化禁用 {name} 时异常: {e}")
            return False
        if ok is False:
            logger.error(
                f"模块 {name} 的禁用状态未能写入 plugin_state.json，重启后会被重新加载"
            )
            return False
        return True

    # ==================== 查询 ====================

    def get_crash_report(self, limit: int = 20) -> Dict[str, Any]:
        """崩溃报告（模块/位置/异常）

        limit 必须夹取。原实现直接 self._crashes[-limit:]：
          limit=0  -> [-0:] 等于全量，一次吐出所有崩溃记录
          limit=-3 -> 变成「跳过前 3 条」，返回的既不是最近 3 条也不是全部
        Python 里 -0 == 0，切片对 0 和负数的语义都不是「取最近 N 条」。
        """
        limit = _coerce_crash_limit(limit)
        return {
            "total": len(self._crashes),
            "disabled_modules": sorted(self._disabled),
            "reload_counts": dict(self._reload_counts),
            "recent": list(reversed(self._crashes[-limit:])),
            "report_file": self.report_file,
        }

    def is_module_ok(self, module: str) -> bool:
        """模块是否可用：本进程内被禁用，或已被持久化禁用，都视为不可用"""
        if module in self._disabled:
            return False
        if self.pm is not None:
            name = module.split(":", 1)[-1]
            try:
                if name in self.pm._persisted.get("disabled", []):
                    return False
            except Exception:
                pass
        return True
