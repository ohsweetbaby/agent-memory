"""记忆领域枚举：类型、作用域与生命周期状态。"""

from enum import Enum


class MemoryType(str, Enum):
    """记忆类型，决定默认重要性、向量化与生命周期策略。"""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PERCEPTUAL = "perceptual"


class Scope(str, Enum):
    """记忆作用域。每条记忆有且仅有一个作用域。"""

    GLOBAL = "global"
    AGENT = "agent"
    USER = "user"
    SESSION = "session"


class MemoryStatus(str, Enum):
    """记忆生命周期状态；删除默认是软删除（DELETED）。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    PURGED = "purged"
