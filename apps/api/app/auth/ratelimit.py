"""Redis 计数：登录失败限流与 API Key 配额。

- 登录：校验前先用 ``SET login:attempt:<ip> NX PX 5000`` 抢串行令牌，同一
  IP 同时只允许一个登录校验在途；拿不到令牌直接 429。失败计数用 Lua
  原子完成 INCR + PEXPIRE + 达阈值加锁 + 清计数，避免 check-then-act 与
  无 TTL 的孤儿键。
- API Key：Lua 原子 INCR + PEXPIRE，按分钟固定窗口限制读写次数。

所有操作都捕获 Redis 异常并按“失败开放”处理：限流服务不可用时业务仍可
继续（登录仍需正确密码），但会在日志中告警。集成测试用真实 Redis 容器
验证计数路径。
"""

from __future__ import annotations

import logging
import secrets
import time
from functools import lru_cache

import redis

from app.config import get_current_settings
from app.errors import RateLimitError

logger = logging.getLogger(__name__)

API_KEY_BUCKET_SECONDS = 60
API_KEY_BUCKET_TTL_MS = 90_000
LOGIN_ATTEMPT_PREFIX = "login:attempt"
LOGIN_ATTEMPT_TTL_MS = 5_000
LOGIN_FAIL_PREFIX = "login:fail"
LOGIN_LOCK_PREFIX = "login:lock"

# KEYS[1]=失败计数，KEYS[2]=锁定标记；ARGV[1]=窗口毫秒，ARGV[2]=阈值，ARGV[3]=锁定毫秒
_LOGIN_FAILURE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
if count >= tonumber(ARGV[2]) then
    redis.call('SET', KEYS[2], '1', 'PX', ARGV[3])
    redis.call('DEL', KEYS[1])
end
return count
"""

# KEYS[1]=计数桶；ARGV[1]=TTL 毫秒
_INCREMENT_WITH_TTL_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return count
"""

# 只释放自己持有的 attempt 令牌，避免误删后来者的令牌。
_RELEASE_ATTEMPT_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


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


def acquire_login_attempt(ip: str) -> str | None:
    """抢占登录串行令牌；已被占用返回 None（调用方直接 429）。"""
    token = secrets.token_hex(8)
    try:
        acquired = get_redis().set(
            f"{LOGIN_ATTEMPT_PREFIX}:{ip}", token, nx=True, px=LOGIN_ATTEMPT_TTL_MS
        )
    except redis.RedisError:
        logger.warning("redis_unavailable", exc_info=True)
        return token  # 失败开放：仍允许登录，但不做并发串行化
    return token if acquired else None


def release_login_attempt(ip: str, token: str) -> None:
    _safe(
        get_redis().eval,
        _RELEASE_ATTEMPT_SCRIPT,
        1,
        f"{LOGIN_ATTEMPT_PREFIX}:{ip}",
        token,
    )


def login_lock_seconds(ip: str) -> int:
    """返回剩余锁定秒数；未锁定返回 0。"""
    ttl = _safe(get_redis().ttl, f"{LOGIN_LOCK_PREFIX}:{ip}")
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else 0


def record_login_failure(ip: str) -> int | None:
    """原子累计失败次数；达到阈值后写入 15 分钟锁并清空计数。"""
    settings = get_current_settings()
    count = _safe(
        get_redis().eval,
        _LOGIN_FAILURE_SCRIPT,
        2,
        f"{LOGIN_FAIL_PREFIX}:{ip}",
        f"{LOGIN_LOCK_PREFIX}:{ip}",
        settings.login_failure_window_seconds * 1000,
        settings.login_failure_limit,
        settings.login_lockout_seconds * 1000,
    )
    return int(count) if count is not None else None


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
    count = _safe(
        get_redis().eval,
        _INCREMENT_WITH_TTL_SCRIPT,
        1,
        redis_key,
        API_KEY_BUCKET_TTL_MS,
    )
    if count is None:
        return
    if int(count) > limit:
        retry_after = API_KEY_BUCKET_SECONDS - int(now % API_KEY_BUCKET_SECONDS)
        raise RateLimitError("API Key 请求过于频繁", retry_after=max(retry_after, 1))
