"""兼容层（v2.4.0 可选）

- AstrBot 兼容层：直接加载 AstrBot 社区插件（register() 命令格式），
  提供 astrbot 模块 shim，转换到 Elysia 工具系统
- Yunzai 兼容层：通过 Node 桥接加载 Yunzai（JS）插件，
  将 BotMessage 事件映射为 Yunzai 事件模型

通过 elysia.yaml 的 compat 段开关，默认关闭。
"""
from .astrbot import AstrBotCompatAdapter
from .yunzai import YunzaiCompatAdapter

__all__ = ["AstrBotCompatAdapter", "YunzaiCompatAdapter"]