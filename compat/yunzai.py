"""Yunzai 兼容层（bate_2.4.0，可选）

启用云崽兼容层时，额外挂载一个 Node 宿主进程（模拟 Yunzai 运行时），
通过 JSON-RPC（stdin/stdout）与 Python 侧通信。

支持的插件格式：
1. 老格式（Yunzai 原生）：目录内 .js 文件 `export class xxx extends plugin`，
   使用全局 plugin 基类（super({event, rule, priority}) + this.reply/setContext/finish + this.e）
2. 新格式（index.js/index.mjs）：无内部别名依赖（#miao 等）的可加载，
   依赖 Yunzai 内部模块的插件会跳过并记录日志

事件模型（shim）：
    e.message   - 消息文本
    e.user_id   - 用户 ID
    e.group_id  - 群组 ID（私聊为 null）
    e.at        - 是否 @Bot
    e.msg       - 原始消息段列表
    e.reply()   - 回复（收集后由 Elysia 统一发送）

启用：elysia.json -> compat.yunzai_enabled: true
需要 Node.js 环境（compat.yunzai_node_bin，默认 node）
"""
import os
import json
import asyncio
import subprocess
from typing import Dict, Any, List, Optional

from ..utils.logger import get_logger

logger = get_logger("YunzaiCompat")

# =====================================================================
# Node 宿主桥接脚本：模拟 Yunzai 运行时（plugin 基类 + 事件分发）
# =====================================================================
BRIDGE_JS = r"""
// ===== Yunzai 运行时 shim =====
const log = (...a) => { try { process.stderr.write('[' + new Date().toISOString() + '] ' + a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };
const __instances = [];
let __lastPerPlugin = {};   // 最近一次 load 的按插件实例统计

// 全局 segment（oicq 段构造占位）
globalThis.segment = {
  text: (t) => ({ type: 'text', data: { text: String(t) } }),
  image: (f) => ({ type: 'image', data: { file: String(f) } }),
  at: (qq) => ({ type: 'at', data: { qq: String(qq) } }),
  face: (id) => ({ type: 'face', data: { id: String(id) } }),
  record: (f) => ({ type: 'record', data: { file: String(f) } }),
  video: (f) => ({ type: 'video', data: { file: String(f) } }),
  button: () => '',
};

// 全局 Bot 占位
globalThis.Bot = { logger: { info: log, log, warn: log, error: log, debug: () => {} } };

// ===== plugin 基类（Yunzai 消息插件） =====
class plugin {
  constructor(config = {}) {
    this.config = config;
    this.name = config.name || 'anonymous';
    this.dsc = config.dsc || '';
    this.event = config.event || 'message';
    this.priority = config.priority ?? 10000;
    this.rule = config.rule || [];
    this.e = null;
    this._replies = [];
    this._ctx = null;
    __instances.push(this);
  }

  reply(message, quote, opts) {
    // 支持 this.reply('文本') / this.reply(['文本']) / this.reply(segment)
    const items = Array.isArray(message) ? message : [message];
    for (const it of items) {
      if (it && typeof it === 'object' && it.type) {
        // segment 对象 -> 文本化
        this._replies.push('[msg:' + (it.data?.text || it.data?.file || it.type) + ']');
      } else {
        this._replies.push(String(it));
      }
    }
    return null;
  }

  setContext(name) { this._ctx = name; }
  finish(name) { if (this._ctx === name) this._ctx = null; }
  getContext() { return this._ctx; }

  // 事件分发：匹配 rule 并执行
  async dispatch(e) {
    this.e = e;
    this._replies = [];
    let handled = false;

    // 上下文模式：有挂起上下文时优先执行对应方法
    if (this._ctx && typeof this[this._ctx] === 'function') {
      const ctxName = this._ctx;
      try { await this[ctxName](); } catch (err) { this._replies.push('[Yunzai 插件错误] ' + err); }
      if (this._ctx === ctxName) this._ctx = null; // 未显式 finish 的上下文执行后清除
      handled = this._replies.length > 0;
      return handled;
    }

    if (this.event !== 'message') return false;
    for (const r of this.rule || []) {
      const reg = r.reg || '';
      let ok = false;
      try { ok = new RegExp(reg).test(e.message || ''); } catch (err) { ok = false; }
      if (!ok) continue;
      // 权限检查
      const perm = r.permission || 'all';
      if (perm === 'master' && !e.is_master) continue;
      if (perm === 'admin' && !e.is_admin) continue;
      // 执行
      const fnc = r.fnc || '';
      if (fnc && typeof this[fnc] === 'function') {
        try { await this[fnc](); } catch (err) { this._replies.push('[Yunzai 插件错误] ' + err); }
        handled = true;
        break;
      }
    }
    return handled;
  }
}
globalThis.plugin = plugin;

// ===== 插件加载 =====
const path = require('path');
const fs = require('fs');

async function importData(p) {
  return import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(p, 'utf-8')).toString('base64'));
}

// 实例化模块中所有 plugin 子类（处理 default 导出与命名导出），返回实例数
async function instantiateFromMod(mod, sourceLabel, pluginLabel) {
  let n = 0;
  const seen = new Set();
  const candidates = [];
  if (mod && mod.default && typeof mod.default === 'function') {
    candidates.push(mod.default);
    seen.add(mod.default);
  }
  for (const k of Object.keys(mod || {})) {
    const cls = mod[k];
    if (typeof cls === 'function' && cls.prototype instanceof plugin && !seen.has(cls)) {
      candidates.push(cls);
    }
  }
  for (const cls of candidates) {
    try { const inst = new cls(); inst._source = pluginLabel || sourceLabel; n++; } catch (e) { log('[yunzai] init fail', sourceLabel, String(e).slice(0, 120)); }
  }
  return n;
}

async function loadPluginDir(dir, forcedLabel) {
  let count = 0;
  const per = {};
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      // 目录式插件：优先 index.*，否则扫描目录内扁平 .js（来源标记 = 插件目录名）
      const label = forcedLabel || ent.name;
      let found = false;
      for (const f of ['index.js', 'index.mjs', 'main.js']) {
        const p = path.join(full, f);
        if (fs.existsSync(p)) {
          try { const n = await instantiateFromMod(await importData(p), p, label); count += n; per[label] = (per[label] || 0) + n; }
          catch (e) { log('[yunzai] skip', full, '(', String(e).slice(0, 120), ')'); }
          found = true;
          break;
        }
      }
      if (!found) {
        for (const f of fs.readdirSync(full)) {
          if (f.endsWith('.js')) {
            const p = path.join(full, f);
            try { const n = await instantiateFromMod(await importData(p), p, label); count += n; per[label] = (per[label] || 0) + n; }
            catch (e) { log('[yunzai] skip', p, '(', String(e).slice(0, 120), ')'); }
          }
        }
      }
    } else if (ent.name.endsWith('.js')) {
      // 扁平式插件（export class xxx extends plugin）
      const label = forcedLabel || ent.name.replace(/\.js$/, '');
      try { const n = await instantiateFromMod(await importData(full), full, label); count += n; per[label] = (per[label] || 0) + n; }
      catch (e) { log('[yunzai] skip', full, '(', String(e).slice(0, 120), ')'); }
    }
  }
  __lastPerPlugin = per;
  return count;
}

// ===== 事件分发 =====
async function dispatchEvent(msg) {
  const masters = msg.masters || [];
  const admins = msg.admins || [];
  const e = {
    message: msg.content || '',
    raw_message: msg.content || '',
    user_id: String(msg.user_id || ''),
    group_id: msg.group_id ? String(msg.group_id) : null,
    is_group: !!msg.group_id,
    at: !!msg.at_bot,
    is_master: masters.includes(String(msg.user_id || '')),
    is_admin: admins.includes(String(msg.user_id || '')),
    msg: [],
    replies: [],
    reply(t) { this.replies.push(String(t)); },
    get_message_str() { return this.message; },
  };

  let handled = false;
  // 按 priority 升序（数字小优先）分发
  const ordered = [...__instances].sort((a, b) => a.priority - b.priority);
  for (const inst of ordered) {
    if (inst._disabled) continue;
    const h = await inst.dispatch(e);
    if (h) {
      // 合并插件实例回复到事件对象
      e.replies.push(...(inst._replies || []));
      handled = true;
      break;
    }
  }
  return { id: msg.id, handled, replies: e.replies };
}

// ===== stdin/stdout JSON-RPC =====
process.stdin.setEncoding('utf8');
let buf = '';
process.stdin.on('data', chunk => {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx);
    buf = buf.slice(idx + 1);
    if (!line.trim()) continue;
    try {
      const msg = JSON.parse(line);
      if (msg.type === 'load') {
        loadPluginDir(msg.dir, msg.source || null).then(n => {
          process.stdout.write(JSON.stringify({ id: msg.id, ok: true, loaded: n, instances: __instances.length, per_plugin: __lastPerPlugin }) + '\n');
        }).catch(err => {
          process.stdout.write(JSON.stringify({ id: msg.id, ok: false, error: String(err) }) + '\n');
        });
      } else if (msg.type === 'event') {
        dispatchEvent(msg).then(out => {
          process.stdout.write(JSON.stringify(out) + '\n');
        });
      } else if (msg.type === 'ping') {
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, pong: true, instances: __instances.length }) + '\n');
      } else if (msg.type === 'unload') {
        // 按来源标记移除实例（单插件卸载）
        const before = __instances.length;
        for (let i = __instances.length - 1; i >= 0; i--) {
          if (__instances[i]._source === msg.source) __instances.splice(i, 1);
        }
        process.stdout.write(JSON.stringify({ id: msg.id, ok: true, removed: before - __instances.length, instances: __instances.length }) + '\n');
      } else if (msg.type === 'set_enabled') {
        // 按来源标记启用/禁用实例参与分发
        let hit = 0;
        for (const inst of __instances) {
          if (inst._source === msg.source) { inst._disabled = !msg.enabled; hit++; }
        }
        process.stdout.write(JSON.stringify({ id: msg.id, ok: hit > 0, instances: __instances.length }) + '\n');
      }
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: -1, error: String(e) }) + '\n');
    }
  }
});

log('yunzai-runtime ready');
"""


class YunzaiCompatAdapter:
    """
    Yunzai 插件兼容适配器（单 Node 宿主进程）

    用法：
        adapter = YunzaiCompatAdapter(plugins_dir="./compat/yunzai_plugins")
        loaded = await adapter.load_all()          # 启动 Node 宿主并加载插件
        result = await adapter.handle_event({...}) # 分发消息事件
        await adapter.unload_all()                 # 停止 Node 宿主
    """

    def __init__(self, plugins_dir: str = "./plugins",
                 node_bin: str = "node", include_dirs: Optional[List[str]] = None):
        self.plugins_dir = plugins_dir
        self.node_bin = node_bin
        self.include_dirs = include_dirs  # 仅加载这些目录（PluginManager 按 kind 过滤）
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._msg_id = 0
        self._lock = asyncio.Lock()
        self._loaded_plugins = 0
        self.load_progress: List[Dict[str, Any]] = []   # 加载过程（每插件状态/实例数）
        self._pending: Dict[int, asyncio.Future] = {}

    def _node_available(self) -> bool:
        try:
            result = subprocess.run([self.node_bin, "--version"],
                                    capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False

    def discover(self) -> List[str]:
        if not os.path.isdir(self.plugins_dir):
            return []
        dirs = [
            d for d in os.listdir(self.plugins_dir)
            if os.path.isdir(os.path.join(self.plugins_dir, d))
        ]
        if self.include_dirs is not None:
            dirs = [d for d in dirs if d in self.include_dirs]
        return sorted(dirs)

    async def load_all(self) -> List[str]:
        """启动 Node 宿主并加载 plugins_dir 下的插件，返回已加载插件名列表（记录加载过程）"""
        if not self._node_available():
            logger.error("Node.js 不可用，Yunzai 兼容层无法启用")
            return []
        names = self.discover()
        if not names:
            logger.warning(f"Yunzai 插件目录为空或不存在: {self.plugins_dir}")
            return []

        await self._start_host()
        # 加载所有插件目录（宿主一次加载全部，模拟真实云崽）
        self.load_progress = [{"name": n, "ok": False, "error": "加载中", "commands": 0, "kind": "yunzai",
                               "enabled": True, "instances": 0}
                              for n in names]
        resp = await self._rpc({"type": "load", "dir": self.plugins_dir}, timeout=30)
        if resp and resp.get("ok"):
            self._loaded_plugins = resp.get("instances", 0)
            per = resp.get("per_plugin", {})
            for item in self.load_progress:
                item["ok"] = True
                item["error"] = ""
                item["instances"] = per.get(item["name"], 0)
            logger.info(f"Yunzai compat loaded {self._loaded_plugins} plugin instances from {len(names)} dirs: {names}")
        else:
            logger.error(f"Yunzai 插件加载失败: {resp}")
            return []
        return names

    async def _start_host(self) -> None:
        """启动 Node 宿主进程"""
        self._proc = await asyncio.create_subprocess_exec(
            self.node_bin, "-e", BRIDGE_JS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 读取 stderr（日志）
        asyncio.get_event_loop().create_task(self._drain_stderr())
        asyncio.get_event_loop().create_task(self._drain_stdout())
        logger.info(f"Yunzai Node host started (pid={self._proc.pid})")

    async def _drain_stderr(self) -> None:
        """消费 stderr 日志，避免缓冲填满"""
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                logger.debug(f"[yunzai-host] {line.decode(errors='ignore').strip()}")
        except Exception:
            pass

    async def _drain_stdout(self) -> None:
        """消费 stdout 响应，路由到 pending future"""
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    resp = json.loads(line.decode("utf-8"))
                    fut = self._pending.pop(resp.get("id", -1), None)
                    if fut and not fut.done():
                        fut.set_result(resp)
                except Exception:
                    pass
        except Exception:
            pass

    async def _rpc(self, msg: Dict[str, Any], timeout: float = 15.0) -> Optional[Dict[str, Any]]:
        """向 Node 宿主发送请求并等待响应"""
        if not self._proc or self._proc.returncode is not None:
            return None
        async with self._lock:
            self._msg_id += 1
            msg["id"] = self._msg_id
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[self._msg_id] = fut
            try:
                self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                await self._proc.stdin.drain()
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(self._msg_id, None)
                logger.warning(f"Yunzai host timeout: {msg.get('type')}")
                return None
            except Exception as e:
                self._pending.pop(self._msg_id, None)
                logger.error(f"Yunzai host error: {e}")
                return None

    async def handle_event(self, event: Dict[str, Any], masters: Optional[List[str]] = None,
                           admins: Optional[List[str]] = None) -> Dict[str, Any]:
        """向 Yunzai 宿主分发消息事件，返回 {handled, replies}"""
        if not self._proc or self._proc.returncode is not None:
            return {"handled": False, "replies": []}
        msg = {
            "type": "event",
            "content": event.get("content", ""),
            "user_id": event.get("user_id", ""),
            "group_id": event.get("group_id"),
            "at_bot": event.get("at_bot", False),
            "masters": masters or [],
            "admins": admins or [],
        }
        resp = await self._rpc(msg, timeout=10)
        if not resp:
            return {"handled": False, "replies": []}
        return {"handled": resp.get("handled", False), "replies": resp.get("replies", [])}

    def get_loaded_plugin_count(self) -> int:
        return self._loaded_plugins

    async def ping(self) -> bool:
        resp = await self._rpc({"type": "ping"}, timeout=5)
        return bool(resp and resp.get("pong"))

    # ==================== 单插件级操作（WebUI 对齐原生层） ====================

    def _is_active(self, plugin_name: str) -> bool:
        """插件是否处于启用状态（按 load_progress 判定）"""
        for p in self.load_progress:
            if p["name"] == plugin_name:
                return bool(p["ok"] and p.get("enabled", True))
        return True

    def _is_loaded(self, plugin_name: str) -> bool:
        return any(p["name"] == plugin_name and p["ok"] for p in self.load_progress)

    async def load(self, name: str) -> bool:
        """加载单个 Yunzai 插件目录（Node 宿主内按来源标记归属实例）"""
        if name not in self.discover():
            return False
        if self._is_loaded(name):
            return True
        if not self._node_available():
            logger.error("Node.js 不可用，Yunzai 兼容层无法启用")
            return False
        try:
            if not self._proc or self._proc.returncode is not None:
                await self._start_host()
        except Exception as e:
            logger.error(f"Yunzai Node 宿主启动失败: {e}")
            return False
        resp = await self._rpc({"type": "load", "dir": os.path.join(self.plugins_dir, name), "source": name}, timeout=30)
        if not (resp and resp.get("ok")):
            logger.error(f"Yunzai 插件加载失败: {name}: {resp}")
            return False
        inst = resp.get("per_plugin", {}).get(name, resp.get("loaded", 0))
        self._loaded_plugins = resp.get("instances", self._loaded_plugins)
        for p in self.load_progress:
            if p["name"] == name:
                p.update({"ok": True, "error": "", "commands": 0, "enabled": True, "instances": inst})
                break
        else:
            self.load_progress.append({"name": name, "ok": True, "error": "", "commands": 0,
                                       "kind": "yunzai", "enabled": True, "instances": inst})
        logger.info(f"Yunzai 兼容插件已加载: {name}（{inst} 个实例）")
        return True

    async def unload(self, name: str) -> bool:
        """内存卸载单个 Yunzai 插件（Node 宿主按来源标记移除实例）"""
        if not self._is_loaded(name):
            return False
        resp = await self._rpc({"type": "unload", "source": name}, timeout=15)
        if not (resp and resp.get("ok")):
            return False
        removed = resp.get("removed", 0)
        self._loaded_plugins = resp.get("instances", max(0, self._loaded_plugins - removed))
        for p in self.load_progress:
            if p["name"] == name:
                p.update({"ok": False, "error": "unloaded", "commands": 0, "enabled": False, "instances": 0})
        logger.info(f"Yunzai 兼容插件已卸载: {name}（移除 {removed} 个实例）")
        return True

    async def enable(self, name: str) -> bool:
        if not self._is_loaded(name):
            return False
        resp = await self._rpc({"type": "set_enabled", "source": name, "enabled": True}, timeout=15)
        if not (resp and resp.get("ok")):
            return False
        for p in self.load_progress:
            if p["name"] == name:
                p["enabled"] = True
        return True

    async def disable(self, name: str) -> bool:
        if not self._is_loaded(name):
            return False
        resp = await self._rpc({"type": "set_enabled", "source": name, "enabled": False}, timeout=15)
        if not (resp and resp.get("ok")):
            return False
        for p in self.load_progress:
            if p["name"] == name:
                p["enabled"] = False
        return True

    def get_state_summary(self) -> List[Dict[str, Any]]:
        """兼容层状态摘要（与原生 PluginManager.get_state_summary 结构对齐）"""
        return [
            {"name": p["name"], "state": "loaded" if p["ok"] else "unloaded",
             "loaded": bool(p["ok"]), "enabled": bool(p.get("enabled", p["ok"])),
             "version": "", "description": f"Yunzai 兼容插件（{p.get('instances', 0)} 实例）",
             "kind": "yunzai"}
            for p in self.load_progress
        ]

    async def unload_all(self) -> None:
        """停止 Node 宿主进程"""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("host stopped"))
        self._pending.clear()
        self._proc = None