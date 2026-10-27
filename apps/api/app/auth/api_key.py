"""API Key：``mem_<prefix>_<secret>``。

- 数据库只保存 Argon2id 哈希与 12 字符前缀；明文只在创建响应中出现一次。
- 前缀唯一且可索引，校验时先按前缀定位，再做 Argon2 校验。
- 三档 scope 是包含关系：``admin`` ⊃ ``write`` ⊃ ``read``。
"""

from __future__ import annotations

import secrets
import string

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.password import hash_password, verify_password
from app.models import ApiKey

KEY_PREFIX = "mem"
PREFIX_LENGTH = 12
SECRET_BYTES = 16  # token_hex(16) -> 32 个十六进制字符
SCOPES: tuple[str, ...] = ("read", "write", "admin")

_PREFIX_ALPHABET = string.ascii_lowercase + string.digits


def _new_prefix() -> str:
    return "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))


def normalize_scopes(scopes: list[str]) -> list[str]:
    """去重并校验；未知 scope 抛 ``ValueError``。"""
    normalized = list(dict.fromkeys(scopes))
    if not normalized:
        raise ValueError("API Key 至少需要一个 scope")
    invalid = [scope for scope in normalized if scope not in SCOPES]
    if invalid:
        raise ValueError(f"未知 scope: {', '.join(invalid)}")
    return normalized


def create_api_key(
    session: Session,
    workspace_id: str,
    name: str,
    scopes: list[str],
    rate_limit: int | None = None,
) -> tuple[ApiKey, str]:
    """创建 Key 并返回 ``(ApiKey, 一次性明文)``；调用方负责提交事务。"""
    normalized = normalize_scopes(scopes)
    prefix = _new_prefix()
    raw = f"{KEY_PREFIX}_{prefix}_{secrets.token_hex(SECRET_BYTES)}"
    key = ApiKey(
        workspace_id=workspace_id,
        name=name,
        prefix=prefix,
        key_hash=hash_password(raw),
        scopes=normalized,
        rate_limit=rate_limit,
    )
    session.add(key)
    session.flush()
    return key, raw


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """拆出 ``(prefix, secret)``；格式不合法返回 None。"""
    parts = raw.strip().split("_")
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def verify_api_key(session: Session, raw: str) -> ApiKey | None:
    """按前缀定位并校验哈希；已吊销、格式错误或摘要不匹配返回 None。"""
    parsed = parse_api_key(raw)
    if parsed is None:
        return None
    prefix, _ = parsed
    key = session.scalars(select(ApiKey).where(ApiKey.prefix == prefix)).first()
    if key is None or key.revoked_at is not None:
        return None
    if not verify_password(raw.strip(), key.key_hash):
        return None
    return key


def has_scope(scopes: list[str], required: str) -> bool:
    """``admin`` 覆盖 ``write`` 与 ``read``。"""
    if required not in SCOPES:
        raise ValueError(f"未知 scope: {required}")
    required_rank = SCOPES.index(required)
    return any(scope in SCOPES and SCOPES.index(scope) >= required_rank for scope in scopes)
