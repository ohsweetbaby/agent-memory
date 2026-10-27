"""Redis 计数：登录失败限流与 API Key 配额。

所有操作都捕获 Redis 异常并按“失败开放”处理：限流服务不可用时业务仍可
继续（登录仍需正确密码），但会在日志中告警。集成测试用真实 Redis 容器
验证计数路径。
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

import redis

from app.config import get_current_settings
from app.errors import RateLimitError

logger = logging.getLogger(__name__)

API_KEY_BUCKET_SECONDS = 60
API_KEY_BUCKET_TTL = 90
LOGIN_FAIL_PREFIX = "login:fail"
LOGIN_LOCK_PREFIX = "login:lock"


@lru_cache(maxsize=8)
def _client(redis_url: str) -> redis.Redis:
    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
    )


def get_redis() -> redis.Redis:
    """当前应用配置对应的 Redis 客户端。"""
    return _client(get_current_settings().redis_url)


def _safe(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except redis.RedisError:
        logger.warning("redis_unavailable", exc_info=True)
        return None


def login_lock_seconds(ip: str) -> int:
    """返回剩余锁定秒数；未锁定返回 0。"""
    ttl = _safe(get_redis().ttl, f"{LOGIN_LOCK_PREFIX}:{ip}")
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else 0


def record_login_failure(ip: str) -> None:
    """累计失败次数；达到阈值后锁定并在锁定期清空计数。"""
    settings = get_current_settings()
    client = get_redis()
    count = _safe(client.incr, f"{LOGIN_FAIL_PREFIX}:{ip}")
    if count is None:
        return
    if int(count) == 1:
        _safe(client.expire, f"{LOGIN_FAIL_PREFIX}:{ip}", settings.login_failure_window_seconds)
    if int(count) >= settings.login_failure_limit:
        _safe(client.set, f"{LOGIN_LOCK_PREFIX}:{ip}", "1", ex=settings.login_lockout_seconds)
        _safe(client.delete, f"{LOGIN_FAIL_PREFIX}:{ip}")


def reset_login_failures(ip: str) -> None:
    _safe(get_redis().delete, f"{LOGIN_FAIL_PREFIX}:{ip}")


def enforce_api_key_rate(
    key_id: str, *, write: bool, override: int | None = None
) -> None:
    """按“每分钟固定窗口”限制单个 Key 的读写次数。

    ``override`` 是 Key 级每分钟上限（读写共用）；未设置时读/写分别取
    ``api_key_read_rpm`` / ``api_key_write_rpm``。
    """
    settings = get_current_settings()
    limit = override if override is not None else (
        settings.api_key_write_rpm if write else settings.api_key_read_rpm
    )
    if limit <= 0:
        return
    now = time.time()
    bucket = int(now // API_KEY_BUCKET_SECONDS)
    kind = "w" if write else "r"
    redis_key = f"api_key:{key_id}:{kind}:{bucket}"
    client = get_redis()
    count = _safe(client.incr, redis_key)
    if count is None:
        return
    if int(count) == 1:
        _safe(client.expire, redis_key, API_KEY_BUCKET_TTL)
    if int(count) > limit:
        retry_after = API_KEY_BUCKET_SECONDS - int(now % API_KEY_BUCKET_SECONDS)
        raise RateLimitError("API Key 请求过于频繁", retry_after=max(retry_after, 1))
