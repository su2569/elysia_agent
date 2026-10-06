"""Memory Adapters"""
from .sqlite_adapter import SQLiteMemory
from .enhanced_adapter import EnhancedMemory

try:
    from .chatdb_adapter import ChatDBMemory
except ImportError:
    ChatDBMemory = None

try:
    from .nodes import MemoryNodeManager, MemoryNode, MemoryEvent, DailySummary, MemoryTheme
except ImportError:
    MemoryNodeManager = None
    MemoryNode = None
    MemoryEvent = None
    DailySummary = None
    MemoryTheme = None

__all__ = ["SQLiteMemory", "EnhancedMemory", "ChatDBMemory", "MemoryNodeManager", "MemoryNode", "MemoryEvent", "DailySummary", "MemoryTheme"]
