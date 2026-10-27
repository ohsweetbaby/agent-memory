"""密码哈希：Argon2id。

参数采用 OWASP 推荐的低内存配置（19 MiB, t=2, p=1），单次校验约数十毫秒，
既能抵御离线爆破，也不会拖慢登录接口。
"""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# 兼容旧导入：provider 密钥加解密也放在 auth 包内。
from app.auth.crypto import decrypt_secret, encrypt_secret

_HASHER = PasswordHasher(
    type=Type.ID,
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(raw: str) -> str:
    """返回 Argon2id 哈希（形如 ``$argon2id$...``）。"""
    return _HASHER.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    """常量时间校验；格式错误或摘要不匹配一律返回 False。"""
    try:
        return _HASHER.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """密码参数升级后可用于登录时透明重哈希。"""
    try:
        return _HASHER.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


__all__ = [
    "decrypt_secret",
    "encrypt_secret",
    "hash_password",
    "needs_rehash",
    "verify_password",
]
