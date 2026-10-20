"""Elysia Agent - 配置系统"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import os
import json
import logging
import yaml


logger = logging.getLogger("elysia.config")


@dataclass
class MemoryConfig:
    """记忆模块配置"""
    enabled: bool = True
    provider: str = "enhanced"       # enhanced | chatdb | sqlite | redis | none
    host: str = "localhost"
    port: int = 17320
    password: str = ""
    db: int = 0
    recent_limit: int = 5
    semantic_topk: int = 5
    semantic_post_topk: int = 3
    vector_retention_days: int = 7
    auto_summarize: bool = True
    summarize_levels: List[str] = field(default_factory=lambda: ["3h", "12h", "24h", "month"])


@dataclass
class LLMConfig:
    """LLM配置"""
    provider: str = "openai"           # openai | deepseek | ollama | custom
    api_key: str = ""
    api_base: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout: int = 60
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536


@dataclass
class EmotionConfig:
    """情绪系统配置（参考MaiBot）"""
    enabled: bool = True
    auto_detect: bool = True
    mood_decay_rate: float = 0.05      # 心情衰减率
    mood_boost_reply: float = 0.1      # 回复后心情提升
    mood_penalty_ignore: float = -0.15 # 被忽略后心情下降
    emotion_history_limit: int = 50
    personality_influence: float = 0.3  # 人格对情绪的影响权重


@dataclass
class ToolConfig:
    """工具系统配置"""
    enabled: bool = True
    max_steps: int = 30
    timeout_per_tool: int = 30
    allow_destructive: bool = False     # 是否允许破坏性操作
    builtin_tools: List[str] = field(default_factory=lambda: [
        "get_current_time",
        "calculator",
        "web_search",
        "remember",
        "recall",
    ])
    blocked_tools: List[str] = field(default_factory=list)


@dataclass
class SubAgentConfig:
    """子代理配置"""
    enabled: bool = True
    max_concurrent: int = 3
    default_max_steps: int = 10


@dataclass
class ToolChainConfig:
    """工具链配置（v2.6.0）"""
    enabled: bool = True              # 是否启用 run_tool_chain 动作
    max_steps: int = 8                # 单链默认步数上限（硬上限 32 在 plan.py 内钳制）
    max_concurrent: int = 5           # 组内并行上限
    overall_timeout: float = 180.0    # 整链超时兜底
    result_cap: int = 20000           # 观测文本体积上限（防上下文挤爆）


@dataclass
class CronConfig:
    """定时任务配置（v2.6.0，阶段二收官）"""
    enabled: bool = True               # 是否启用定时任务调度器
    state_file: str = "./data/cron_state.json"   # 持久化文件
    poll_interval: float = 30.0        # 调度循环扫描间隔（秒）
    misfire_grace_sec: float = 300.0   # 错过触发点的补跑宽限（超出则跳过）


@dataclass
class SnapshotConfig:
    """沙箱快照回滚配置（v2.6.0）

    写操作前自动打点，改坏后可 /snap rollback 恢复到写前状态。
    """
    enabled: bool = True               # 是否启用快照回滚
    depth: int = 3                     # 滚动快照轮数（1-9）
    monitor: List[str] = field(default_factory=lambda: [
        "elysia.json", "elysia_agent/config/",
    ])                                  # 监控的文件/目录（相对项目根）
    backup_dir: str = ""               # 持久化备份目录（空=纯内存）
    hook_enabled: bool = True          # 是否注册 before_act 自动打点钩子


@dataclass
class ServerConfig:
    """服务端配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    ws_port: int = 8081
    tcp_port: int = 17320
    max_clients: int = 32
    heartbeat_interval: int = 30
    enable_cors: bool = True
    auth_token: str = ""
    # 写接口访问控制：local_only（仅本机，默认）| token（需 auth_token）| off（不拦截）
    write_guard: str = "local_only"


@dataclass
class PromptGuardConfig:
    """提示词防护配置（参考 Anti-Prompt Injector）"""
    enabled: bool = True
    mode: str = "intercept"       # sentry | aegis | scorch | intercept
    threshold: float = 30.0       # 触发阈值 0-100
    enable_blacklist: bool = True
    blacklist_duration: int = 60   # 自动封禁时长（分钟，0=永久）
    whitelist: List[str] = field(default_factory=list)
    enable_llm_review: bool = False
    enable_anti_harassment: bool = True
    sanitize_enabled: bool = True
    incident_history_size: int = 100


@dataclass
class MemoryNodeConfig:
    """记忆节点配置（参考 APLR）"""
    enabled: bool = True
    enable_daily_summary: bool = True
    enable_auto_recall: bool = True
    enable_theme_clustering: bool = True
    auto_inject_on_new_session: bool = True
    inject_recent_days: int = 3     # 新会话注入近N天重要事件
    inject_summary_days: int = 7    # 新会话注入近N天总结
    recall_weights: Dict[str, float] = field(default_factory=lambda: {
        "semantic": 1.0,
        "importance": 1.0,
        "emotion": 1.0,
        "time_decay": 1.0,
        "rarity": 1.0
    })
    max_nodes_per_recall: int = 5   # 每次回忆最多提供节点数
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    date_split_hour: int = 4        # 每日总结日期分割点（凌晨4点）


@dataclass
class SafetyConfig:
    """安全与提示词防护配置"""
    enable_prompt_guard: bool = True
    system_prompt_priority: str = "highest"
    prevent_user_system_override: bool = True
    max_user_message_length: int = 8000
    enable_content_filter: bool = True
    blocked_keywords: List[str] = field(default_factory=list)
    max_tool_calls_per_session: int = 50
    sanitize_memory_before_store: bool = True
    memory_max_size_mb: int = 100


@dataclass
class LogConfig:
    """日志配置"""
    level: str = "INFO"
    format: str = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    file: Optional[str] = None
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5


@dataclass
class PersonalityConfig:
    """人格配置"""
    name: str = "Elysia"
    description: str = "温柔、好奇、乐于助人的AI助手，喜欢探索新事物"
    style: str = "自然、带emoji、偶尔俏皮，像朋友一样聊天"
    background: str = "来自虚拟世界的守护者，拥有长期记忆能力"


@dataclass
class ConcurrencyConfig:
    """并发处理配置"""
    max_concurrent_sessions: int = 100
    max_concurrent_llm: int = 5
    max_concurrent_tools: int = 5
    rate_limit_requests: int = 30
    rate_limit_window: int = 60
    rate_limit_burst: int = 5
    memory_queue_batch: int = 10
    memory_queue_flush_interval: float = 1.0
    llm_cache_ttl: int = 60
    enable_parallel_tools: bool = True
    enable_batch_llm: bool = True
    enable_memory_queue: bool = True


@dataclass
class PermissionsConfig:
    """权限管理 + 高危行为授权（v2.4.0）"""
    enabled: bool = True
    owner_ids: List[str] = field(default_factory=list)   # 所有者 user_id
    admin_ids: List[str] = field(default_factory=list)   # 管理员 user_id
    dangerous_tools: List[str] = field(default_factory=lambda: [
        "shell", "execute", "execute_command", "file_write", "file_delete",
        "remove", "delete", "sudo", "transfer", "send_money", "install_package",
        "run_code", "system",
    ])
    require_confirm: bool = True        # 高危操作需授权确认
    auth_ttl: int = 300                 # 授权有效期（秒）
    max_pending: int = 20               # 挂起授权请求上限


@dataclass
class SelfCheckConfig:
    """程序异常自检（反未知漏洞利用 / 反未授权行为，v2.4.0）"""
    enabled: bool = True
    check_on_start: bool = True
    interval: int = 60                  # 运行期自检间隔（秒）
    block_unauthorized: bool = True     # 发现未授权行为时拦截
    scan_depth: int = 2                 # sys.path 扫描深度
    report_file: str = "self_check_report.json"


@dataclass
class CompatConfig:
    """兼容层配置（AstrBot / Yunzai 可选，v2.4.0）"""
    astrbot_enabled: bool = False
    astrbot_plugins_dir: str = "./compat/astrbot_plugins"
    yunzai_enabled: bool = False
    yunzai_plugins_dir: str = "./compat/yunzai_plugins"
    yunzai_node_bin: str = "node"


@dataclass
class PromptConfig:
    """智能提示词管理（防臃肿，v2.4.0）"""
    max_tokens: Optional[int] = 8192    # 系统提示词预算（None=不限）
    include_skills: bool = False        # 是否注入技能索引
    enable_ranking: bool = True         # 常用度排名
    auto_prune: bool = True             # 预算超限自动裁剪


@dataclass
class OneBotConfig:
    """OneBot v11 连接配置（QQ 官方机器人，正向/反向两种模式）"""
    enabled: bool = False
    mode: str = "reverse"            # reverse=客户端连 QQ 服务 / forward=服务端等 QQ 连接 / both=两者
    host: str = "127.0.0.1"          # 反向模式：QQ WS 服务地址
    port: int = 1005                 # 反向模式：QQ WS 服务端口
    forward_host: str = "0.0.0.0"    # 正向模式：监听地址
    forward_port: int = 1006         # 正向模式：监听端口
    token: str = ""
    allowed_groups: List[str] = field(default_factory=lambda: ["1049279188"])


@dataclass
class PlatformConfig:
    """通用平台实例配置（对齐 AstrBot cmd_config.json 的 platform 数组）

    同一 type 可配多个（靠 id 区分），enable=False 则该实例跳过加载。
    不写的 type 自然不会加载。各平台专属参数随 type 而变。
    """
    id: str = ""
    type: str = ""                 # onebot_ws | onebot_ws_server | http | telegram | discord | qq_official
    enable: bool = True
    # ---- onebot_ws / onebot_ws_server ----
    mode: str = "reverse"          # reverse=客户端连 QQ 服务 / forward=服务端等连接 / both=两者
    host: str = "127.0.0.1"        # 反向：QQ WS 服务地址
    port: int = 1005               # 反向：QQ WS 服务端口
    forward_host: str = "0.0.0.0"  # 正向：监听地址
    forward_port: int = 1006       # 正向：监听端口
    token: str = ""
    allowed_groups: List[str] = field(default_factory=lambda: ["1049279188"])
    # ---- qq_official（官方开放平台，占位给布洛妮娅/昔涟机器人）----
    appid: str = ""
    secret: str = ""
    base_url: str = "https://api.sgroup.qq.com"
    intents: List[str] = field(default_factory=list)
    sandbox: bool = True
    enable_group_c2c: bool = True
    enable_guild_direct_message: bool = True
    # ---- http / telegram / discord ----
    auth_token: str = ""


@dataclass
class MCPServerConfig:
    """MCP 服务器连接配置（阶段二：core/mcp_client.py 消费）

    transport: stdio | http | sse
    - stdio: command/args/env/cwd（子进程 + 换行分隔 JSON-RPC）
    - http:  streamable-http 单端点（MCP 2025-03-26+），自动维护 Mcp-Session-Id
    - sse:   遗留 SSE 传输（MCP 2024-11-05），GET 长连接 + POST 回发
    """
    name: str = ""                    # 工具前缀：注册为 {name}__{tool}
    transport: str = "stdio"
    enabled: bool = True
    # ---- stdio ----
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # ---- http / sse ----
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    # ---- 其他 ----
    timeout: int = 60                 # 该服务器单次请求超时（秒，1min 默认）
    kill_timeout: int = 180           # 该服务器强制停止阈值（秒，3min 默认）
    async_exec: bool = False          # 该服务器异步执行开关


@dataclass
class MCPConfig:
    """MCP 客户端全局配置（阶段二）"""
    enabled: bool = False             # 默认关闭：无服务器配置时零开销
    strict_command_validation: bool = True   # stdio 命令注入防御校验
    max_tools_per_server: int = 200   # 防恶意服务器淹没注册表
    connect_timeout: int = 30
    request_timeout: int = 60         # 单次请求小超时（默认 1 分钟）
    kill_timeout: int = 180           # 强制停止/强停阈值（默认 3 分钟）
    async_exec: bool = False          # 异步执行开关（远端调用走独立任务，不阻塞主链路）
    servers: List[MCPServerConfig] = field(default_factory=list)


@dataclass
class Settings:
    """
    Elysia Agent 全局配置

    支持从文件加载，优先级：
    环境变量 > 配置文件 > 默认值
    """
    debug: bool = False
    plugins_dir: str = "./plugins"
    data_dir: str = "./data"

    memory: MemoryConfig = field(default_factory=MemoryConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    tool: ToolConfig = field(default_factory=ToolConfig)
    subagent: SubAgentConfig = field(default_factory=SubAgentConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    guard: PromptGuardConfig = field(default_factory=PromptGuardConfig)
    memory_node: MemoryNodeConfig = field(default_factory=MemoryNodeConfig)
    log: LogConfig = field(default_factory=LogConfig)
    personality: PersonalityConfig = field(default_factory=PersonalityConfig)
    # 多人格库：每项 {id, name, description, style, background}；active_personality 为当前生效 id
    personalities: List[Dict[str, Any]] = field(default_factory=list)
    active_personality: str = ""
    concurrency: Optional[ConcurrencyConfig] = None  # None = 使用旧实现（无并发控制）
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    self_check: SelfCheckConfig = field(default_factory=SelfCheckConfig)
    compat: CompatConfig = field(default_factory=CompatConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    platform: List[PlatformConfig] = field(default_factory=list)  # 通用平台实例列表
    mcp: MCPConfig = field(default_factory=MCPConfig)             # MCP 客户端（阶段二）
    tool_chain: ToolChainConfig = field(default_factory=ToolChainConfig)  # 工具链（阶段二 v2.6.0）
    cron: CronConfig = field(default_factory=CronConfig)          # 定时任务（阶段二 v2.6.0 收官）
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)  # 沙箱快照回滚

    def __post_init__(self):
        # 所有构造路径（默认构造 / from_dict / from_file）都应用环境变量覆盖
        self._apply_env_overrides()

    @classmethod
    def from_file(cls, path: str) -> "Settings":
        """从配置文件加载（JSON 优先，兼容 YAML）

        配置读取经沙箱校验（沙箱在配置加载前激活）。
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")

        from ..core.sandbox import sandbox
        content = sandbox.read_config(path)
        ext = os.path.splitext(path)[1].lower()

        if ext in (".json",):
            raw = json.loads(content) or {}
        else:
            # 兼容 YAML（pyyaml 未安装时回退）
            try:
                raw = yaml.safe_load(content) or {}
            except ImportError:
                raise ImportError("解析 YAML 配置需要 pyyaml: pip install pyyaml")

        # 沙箱校验危险字段
        if not sandbox.validate_config_data(raw, path):
            raise PermissionError(f"沙箱拦截: 配置含危险字段 {path}")

        return cls.from_dict(raw)

    @classmethod
    def from_json_file(cls, path: str) -> "Settings":
        """从 JSON 配置加载"""
        if not path.endswith(".json"):
            path = path + ".json"
        return cls.from_file(path)

    def save(self, path: str) -> None:
        """将当前配置写回 JSON 文件

        写路径与读路径对称校验（反恶意管理员/恶意配置）：拒绝路径穿越与系统危险路径。
        """
        from ..core.sandbox import sandbox
        bad = sandbox.check_path(path)
        if bad:
            raise PermissionError(f"沙箱拦截: 保存配置路径 {path} ({bad})")

        data = self.to_dict()
        # concurrency=None 时导出为 null，写文件时省略，保持配置整洁
        if data.get("concurrency") is None:
            data.pop("concurrency", None)
        if not path.endswith((".json", ".yaml", ".yml")):
            path = path + ".json"
        from pathlib import Path
        if path.endswith((".yaml", ".yml")):
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        Path(path).write_text(text, encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        """从字典加载配置"""
        settings = cls()

        # 基础字段
        # 基础字段同样要走类型校正：直接 data.get() 会让 "debug": "true"
        # 原样存成字符串（truthy），"plugins_dir": 123 也会一路带到下游。
        if "debug" in data:
            settings.debug = _coerce_field_value(settings.debug, data["debug"], "debug")
        if "plugins_dir" in data:
            settings.plugins_dir = _coerce_field_value(
                settings.plugins_dir, data["plugins_dir"], "plugins_dir")
        if "data_dir" in data:
            settings.data_dir = _coerce_field_value(
                settings.data_dir, data["data_dir"], "data_dir")

        # 子配置
        if "memory" in data:
            settings.memory = _dict_to_dataclass(settings.memory, data["memory"])
        if "llm" in data:
            settings.llm = _dict_to_dataclass(settings.llm, data["llm"])
        if "emotion" in data:
            settings.emotion = _dict_to_dataclass(settings.emotion, data["emotion"])
        if "tool" in data:
            settings.tool = _dict_to_dataclass(settings.tool, data["tool"])
        if "subagent" in data:
            settings.subagent = _dict_to_dataclass(settings.subagent, data["subagent"])
        if "server" in data:
            settings.server = _dict_to_dataclass(settings.server, data["server"])
        if "safety" in data:
            settings.safety = _dict_to_dataclass(settings.safety, data["safety"])
        if "guard" in data:
            settings.guard = _dict_to_dataclass(settings.guard, data["guard"])
        if "memory_node" in data:
            settings.memory_node = _dict_to_dataclass(settings.memory_node, data["memory_node"])
        if "log" in data:
            settings.log = _dict_to_dataclass(settings.log, data["log"])
        if "personality" in data:
            if isinstance(data["personality"], dict):
                settings.personality = _dict_to_dataclass(settings.personality, data["personality"])
        # 多人格库
        if isinstance(data.get("personalities"), list):
            settings.personalities = [
                dict(item) for item in data["personalities"] if isinstance(item, dict)
            ]
        if isinstance(data.get("active_personality"), str):
            settings.active_personality = data["active_personality"]
        if "concurrency" in data:
            if isinstance(data["concurrency"], dict):
                if settings.concurrency is None:
                    settings.concurrency = ConcurrencyConfig()
                settings.concurrency = _dict_to_dataclass(settings.concurrency, data["concurrency"])
        if "permissions" in data:
            settings.permissions = _dict_to_dataclass(settings.permissions, data["permissions"])
        if "self_check" in data:
            settings.self_check = _dict_to_dataclass(settings.self_check, data["self_check"])
        if "compat" in data:
            settings.compat = _dict_to_dataclass(settings.compat, data["compat"])
        if "prompt" in data:
            settings.prompt = _dict_to_dataclass(settings.prompt, data["prompt"])
        if "onebot" in data:
            settings.onebot = _dict_to_dataclass(settings.onebot, data["onebot"])
        if "tool_chain" in data:
            settings.tool_chain = _dict_to_dataclass(settings.tool_chain, data["tool_chain"])
        if "cron" in data:
            settings.cron = _dict_to_dataclass(settings.cron, data["cron"])
        if "snapshot" in data:
            settings.snapshot = _dict_to_dataclass(settings.snapshot, data["snapshot"])
        if "mcp" in data:
            mcp_raw = data["mcp"]
            if isinstance(mcp_raw, dict):
                mcp_raw = dict(mcp_raw)  # copy，避免修改调用方字典
                servers_raw = mcp_raw.pop("servers", None) or []
                settings.mcp = _dict_to_dataclass(settings.mcp, mcp_raw)
                for item in servers_raw:
                    if isinstance(item, dict):
                        settings.mcp.servers.append(
                            _dict_to_dataclass(MCPServerConfig(), item)
                        )
        if "platform" in data:
            # 通用平台实例列表（列表项按 id 归并去重）
            raw_list = data.get("platform") or []
            existing = {p.id: p for p in settings.platform if p.id}
            merged = []
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                pid = item.get("id", "")
                if pid and pid in existing:
                    merged.append(_dict_to_dataclass(existing[pid], item))
                else:
                    merged.append(_dict_to_dataclass(PlatformConfig(), item))
            settings.platform = merged

        # 环境变量覆盖
        settings._apply_env_overrides()

        # 多人格：把当前生效人格同步到 personality 段
        settings.resolve_active_personality()

        return settings

    def resolve_active_personality(self) -> None:
        """将 personalities 中当前生效的人格同步到 personality 段。

        - 未配置多人格库时不做任何处理，保持旧行为（单一 personality）。
        - active_personality 缺失或找不到时，回退到第一个并回写 active_personality。
        """
        if not self.personalities:
            return
        target = None
        if self.active_personality:
            for item in self.personalities:
                if item.get("id") == self.active_personality:
                    target = item
                    break
        if target is None:
            target = self.personalities[0]
            self.active_personality = str(target.get("id") or "")
        for key in ("name", "description", "style", "background"):
            value = target.get(key)
            if isinstance(value, str) and value != "":
                setattr(self.personality, key, value)

    def _apply_env_overrides(self):
        """环境变量覆盖配置"""
        # 主环境变量名（与 .env.example 一致）
        env_map = {
            "ELYSIA_LLM_API_KEY": ("llm", "api_key"),
            "ELYSIA_LLM_MODEL": ("llm", "model"),
            "ELYSIA_LLM_API_BASE": ("llm", "api_base"),
            "ELYSIA_LLM_PROVIDER": ("llm", "provider"),
            "ELYSIA_MEMORY_ENABLED": ("memory", "enabled"),
            "ELYSIA_MEMORY_HOST": ("memory", "host"),
            "ELYSIA_MEMORY_PORT": ("memory", "port"),
            "ELYSIA_GUARD_MODE": ("guard", "mode"),
            "ELYSIA_SERVER_PORT": ("server", "port"),
            "ELYSIA_AUTH_TOKEN": ("server", "auth_token"),
            "ELYSIA_DEBUG": ("debug", None),
        }
        # 历史别名（向后兼容，主名未设置时才生效）
        aliases = {
            "ELYSIA_LLM_BASE": "ELYSIA_LLM_API_BASE",
            "ELYSIA_SERVER_AUTH": "ELYSIA_AUTH_TOKEN",
        }

        for env_key, (section, key) in env_map.items():
            val = os.getenv(env_key)
            if val is None:
                # 主名未设置时回退到历史别名
                alias_for = {v: k for k, v in aliases.items()}
                legacy_key = alias_for.get(env_key)
                if legacy_key:
                    val = os.getenv(legacy_key)
            if val is not None:
                section_obj = getattr(self, section)
                if key is None:
                    # 直接设置顶层属性
                    if section == "debug":
                        setattr(self, section, _coerce_bool(val))
                else:
                    current = getattr(section_obj, key)
                    if isinstance(current, bool):
                        setattr(section_obj, key, _coerce_bool(val))
                    elif isinstance(current, int):
                        # 注意：环境变量来自外部，格式完全不可信。
                        # 早前直接 int(val)，`ELYSIA_SERVER_PORT=abc` 会让
                        # Settings.from_file 抛 ValueError，进程在「加载配置」
                        # 阶段就崩掉——连日志都还没初始化，用户看不到任何原因。
                        _iv = _coerce_int(val)
                        if _iv is None:
                            logger.warning(
                                "环境变量 %s=%r 不是合法整数，已忽略，保留 %s.%s=%r",
                                env_key, val, section, key, current)
                        elif key == "port" and not (1 <= _iv <= 65535):
                            logger.warning(
                                "环境变量 %s=%r 超出端口范围 1-65535，已忽略，保留 %s.%s=%r",
                                env_key, val, section, key, current)
                        else:
                            setattr(section_obj, key, _iv)
                    elif isinstance(current, float):
                        _fv = _coerce_float(val)
                        if _fv is None:
                            logger.warning(
                                "环境变量 %s=%r 不是合法浮点数，已忽略，保留 %s.%s=%r",
                                env_key, val, section, key, current)
                        else:
                            setattr(section_obj, key, _fv)
                    else:
                        setattr(section_obj, key, val)

    def validate(self) -> List[str]:
        """验证配置有效性，返回错误列表"""
        errors = []

        if self.llm.provider == "openai" and not self.llm.api_key:
            errors.append("LLM API Key 未设置（请设置 ELYSIA_LLM_API_KEY 或在配置文件中填写）")

        if self.guard.threshold < 0 or self.guard.threshold > 100:
            errors.append("Guard threshold 必须在 0-100 之间")

        if self.tool.max_steps < 1 or self.tool.max_steps > 100:
            errors.append("tool.max_steps 必须在 1-100 之间")

        if self.safety.max_user_message_length < 100:
            errors.append("max_user_message_length 至少为 100")

        if self.server.port == self.server.ws_port:
            errors.append("HTTP 端口和 WebSocket 端口不能相同")

        return errors

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典"""
        from dataclasses import asdict
        return asdict(self)


def _coerce_field_value(current, value, label: str = ""):
    """按字段当前值的类型，把配置里的原始值校正成该类型。

    背景：`_dict_to_dataclass` 原来只判断「是不是嵌套 dataclass」，
    标量一律原样 setattr。于是 `"memory": {"enabled": "false"}` 会存成
    字符串 `"false"`——它是 truthy，记忆功能被反着执行，且不报错、不打日志。
    `tool.max_steps: "20"` 更直接，`validate()` 里 `< 1` 当场抛 TypeError。

    配置文件是手写的，类型写错太常见。这里统一按「目标字段的真实类型」转换：
      - 转换失败 → 保留原值并记一条 WARNING，绝不让加载阶段抛异常
      - 目标值为 None（Optional 字段）→ 无从判断类型，原样放行
    """
    if current is None:
        return value

    # bool 必须排在 int 之前：isinstance(True, int) 为 True
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off", ""):
                return False
        logger.warning("配置项 %s 期望 bool，收到 %r，已保留原值 %r", label, value, current)
        return current

    if isinstance(current, int):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        iv = _coerce_int(value)
        if iv is None:
            logger.warning("配置项 %s 期望 int，收到 %r，已保留原值 %r", label, value, current)
            return current
        return iv

    if isinstance(current, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        fv = _coerce_float(value)
        if fv is None:
            logger.warning("配置项 %s 期望 float，收到 %r，已保留原值 %r", label, value, current)
            return current
        return fv

    if isinstance(current, str):
        # str 字段原样放行：把数字 20 强行转成 "20" 反而会丢信息
        # （log.level 写数字时 setup_logging 认 int，转成字符串就退化成 INFO 了）。
        # 下游消费方对非字符串都已做了容错，这里不做二次加工。
        return value

    return value


def _dict_to_dataclass(instance, updates: Dict[str, Any], _prefix: str = ""):
    """辅助函数：用字典更新 dataclass 实例（支持嵌套）"""
    from dataclasses import is_dataclass
    for key, value in updates.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        label = f"{_prefix}{key}"
        if is_dataclass(current) and not isinstance(current, type) and isinstance(value, dict):
            _dict_to_dataclass(current, value, _prefix=f"{label}.")
        elif isinstance(current, (list, tuple)) and isinstance(value, list):
            setattr(instance, key, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            setattr(instance, key, {**current, **value})
        else:
            setattr(instance, key, _coerce_field_value(current, value, label))
    return instance


def _coerce_bool(value) -> bool:
    """环境变量转 bool：只认 true/1/yes/on，其余为 False（保持原有语义）。"""
    try:
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    except Exception:
        return False


def _coerce_int(value):
    """环境变量转 int：失败返回 None（调用方保留原值）。

    环境变量是外部输入，可能为空串、带空格、写成 "8080.0" 或纯手误。
    任何情况都不能让配置加载抛异常——那会让进程起不来且无从排查。
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None


def _coerce_float(value):
    """环境变量转 float：失败返回 None。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
