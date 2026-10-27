"""作用域上下文与可见性解析。

可见性遵循「默认最小可见」原则：只合并当前上下文真实存在的作用域标识符，
`global` 始终可见；缺少某个标识符时，对应作用域不可见。
"""

from __future__ import annotations

from dataclasses import dataclass

from memory_core.types import Scope

# (作用域, ScopeContext 上的标识符字段)，顺序即返回结果的规范顺序。
_SCOPE_IDENTIFIER_FIELDS: tuple[tuple[Scope, str], ...] = (
    (Scope.AGENT, "agent_id"),
    (Scope.USER, "end_user_id"),
    (Scope.SESSION, "session_id"),
)


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """一次检索所携带的上下文标识；workspace 必填，其余缺省即不参与可见性。"""

    workspace_id: str
    agent_id: str | None = None
    end_user_id: str | None = None
    session_id: str | None = None


def visible_scopes(
    ctx: ScopeContext,
    requested: list[Scope] | None = None,
) -> list[tuple[Scope, str | None]]:
    """解析当前上下文可见的 ``(scope, scope_id)`` 列表。

    ``requested`` 只做过滤而不是授权：它既不能让上下文中缺失的标识符变为可见，
    也不能排除始终可见的 ``global``。返回顺序固定为 global → agent → user → session。
    空白字符串视为缺失标识符，避免拼出无意义的作用域。
    """
    selected = set(requested) if requested is not None else None
    visible: list[tuple[Scope, str | None]] = [(Scope.GLOBAL, None)]

    for scope, field_name in _SCOPE_IDENTIFIER_FIELDS:
        if selected is not None and scope not in selected:
            continue
        identifier = getattr(ctx, field_name)
        if identifier and identifier.strip():
            visible.append((scope, identifier))

    return visible
