"""Elysia Agent Loop - 状态机定义"""
from enum import Enum, auto

class LoopState(Enum):
    """主循环状态机"""
    IDLE = "idle"                    # 空闲
    PREPARE = "prepare"              # 记忆检索与注入（注入点A）
    THINK = "think"                  # LLM推理
    ACT = "act"                      # 执行工具/子代理
    OBSERVE = "observe"              # 处理结果（注入点B）
    DELEGATING = "delegating"        # 等待子代理返回
    FINALIZE = "finalize"            # 写入被动记忆
    DONE = "done"
    ERROR = "error"

class MessageType(Enum):
    """消息类型"""
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    AT = "at"
    REPLY = "reply"
    FORWARD = "forward"

class EmotionState(Enum):
    """情绪状态（参考MaiBot）"""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    CURIOUS = "curious"
    BORED = "bored"
    EXCITED = "excited"
    ANXIOUS = "anxious"
    LOVING = "loving"

class Priority(Enum):
    """钩子优先级"""
    HIGHEST = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    LOWEST = 4
