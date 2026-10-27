"""Provider 密钥的对称加密。

Fernet 密钥由 ``SECRET_KEY`` 经 SHA-256 派生，进程内缓存；同一份配置下
``encrypt_secret`` / ``decrypt_secret`` 可往返。密文为 Fernet token，
不含明文片段。
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_current_settings


@lru_cache(maxsize=8)
def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _active_fernet() -> Fernet:
    return _fernet(get_current_settings().secret_key)


def encrypt_secret(raw: str) -> bytes:
    """加密 Provider 密钥；返回值直接写入 ``provider_config.encrypted_secret``。"""
    return _active_fernet().encrypt(raw.encode("utf-8"))


def decrypt_secret(blob: bytes) -> str:
    """解密 Provider 密钥；密文被篡改时抛出 ``InvalidToken``。"""
    return _active_fernet().decrypt(blob).decode("utf-8")
