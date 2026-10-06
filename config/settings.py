"""Elysia Agent - 配置系统"""
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import os
import json
import yaml


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
    concurrency: Optional[ConcurrencyConfig] = None  # None = 使用旧实现（无并发控制）
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    self_check: SelfCheckConfig = field(default_factory=SelfCheckConfig)
    compat: CompatConfig = field(default_factory=CompatConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    onebot: OneBotConfig = field(default_factory=OneBotConfig)

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
        with open(path, "w", encoding="utf-8") as f:
            if path.endswith((".yaml", ".yml")):
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        """从字典加载配置"""
        settings = cls()

        # 基础字段
        settings.debug = data.get("debug", settings.debug)
        settings.plugins_dir = data.get("plugins_dir", settings.plugins_dir)
        settings.data_dir = data.get("data_dir", settings.data_dir)

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

        # 环境变量覆盖
        settings._apply_env_overrides()

        return settings

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
                        setattr(self, section, val.lower() in ("true", "1", "yes"))
                else:
                    current = getattr(section_obj, key)
                    if isinstance(current, bool):
                        setattr(section_obj, key, val.lower() in ("true", "1", "yes"))
                    elif isinstance(current, int):
                        setattr(section_obj, key, int(val))
                    elif isinstance(current, float):
                        setattr(section_obj, key, float(val))
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


def _dict_to_dataclass(instance, updates: Dict[str, Any]):
    """辅助函数：用字典更新 dataclass 实例（支持嵌套）"""
    from dataclasses import is_dataclass
    for key, value in updates.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and not isinstance(current, type) and isinstance(value, dict):
            _dict_to_dataclass(current, value)
        elif isinstance(current, (list, tuple)) and isinstance(value, list):
            setattr(instance, key, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            setattr(instance, key, {**current, **value})
        else:
            setattr(instance, key, value)
    return instance
