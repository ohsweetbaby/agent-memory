"""Agent Memory stdio MCP 薄桥。"""

from __future__ import annotations

__all__ = ["build_server", "call_remote", "main"]


def __getattr__(name: str):
    if name in __all__:
        from memory_mcp import __main__ as _entry

        return getattr(_entry, name)
    raise AttributeError(name)
