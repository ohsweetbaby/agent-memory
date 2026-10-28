"""模型 Provider 管理与连通性探测。

设计要点：

- Provider 属于全局配置，仅管理员可读写；数据库中的 ``encrypted_secret``
  使用 :mod:`app.auth.crypto` 加密，列表/详情只返回 ``has_secret``。
- 更新时 ``api_key`` 留空表示不修改；只有显式 ``clear_secret`` 才删除。
- 连通性探测最多 5 秒，失败原因必须是可读中文，且任何日志/响应都不包含密钥。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import audit_actor
from app.auth.crypto import decrypt_secret, encrypt_secret
from app.auth.session import AuthContext, require_scope
from app.config import get_current_settings
from app.db import get_db
from app.errors import BadRequestError, ConflictError, NotFoundError
from app.models import ProviderConfig
from app.schemas.memory import ensure_no_nul
from app.schemas.ops import (
    PROVIDER_TEST_TIMEOUT_SECONDS,
    OpsPolicy,
    ProviderCreate,
    ProviderTestRequest,
    ProviderTestResult,
    ProviderUpdate,
)
from app.services.audit import write_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/providers", tags=["providers"])

#: 只探测 OpenAI 兼容的模型列表；其他 404 时仅在缺少 /v1 时尝试补一次。
_MODELS_PATH = "/models"

#: 云厂商 metadata 服务主机名；loopback/私网仍允许（本地 Ollama）。
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
        "metadata.azure.internal",
        "metadata.oraclecloud.com",
        "metadata.alibabacloud.com",
        "metadata.tencentyun.com",
    }
)

#: 非 link-local 但已知的云 metadata 地址（阿里云/EC2 IPv6）。
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


def _provider_payload(provider: ProviderConfig) -> dict[str, Any]:
    """列表/详情安全视图：绝不读取或回传密文。"""
    return {
        "id": provider.id,
        "kind": provider.kind,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.model,
        "dim": provider.dim,
        "extra": dict(provider.extra or {}),
        "enabled": bool(provider.enabled),
        "has_secret": provider.encrypted_secret is not None,
    }


def _policy_payload() -> OpsPolicy:
    settings = get_current_settings()
    return OpsPolicy(
        decay_archive_threshold=settings.decay_archive_threshold,
        purge_retention_days=settings.purge_retention_days,
        review_retention_days=settings.review_retention_days,
        audit_retention_days=settings.audit_retention_days,
        consolidation_idle_minutes=settings.consolidation_idle_minutes,
        consolidation_nightly=(
            f"{settings.consolidation_nightly_hour:02d}:"
            f"{settings.consolidation_nightly_minute:02d} UTC"
        ),
        search_context_budget_tokens=settings.search_context_budget_tokens,
        search_query_rewrite=settings.search_query_rewrite,
    )


def _normalize_base_url(raw: str | None) -> str:
    """校验 Base URL 只包含 http(s)，并拒绝内嵌账号密码。"""
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise BadRequestError("Base URL 不能为空", code="invalid_base_url")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise BadRequestError("Base URL 格式无效", code="invalid_base_url") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BadRequestError(
            "Base URL 必须是 http:// 或 https:// 地址", code="invalid_base_url"
        )
    if parsed.username or parsed.password:
        raise BadRequestError(
            "Base URL 不能包含账号密码，请使用 API Key", code="invalid_base_url"
        )
    return text


def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """禁止 link-local 与已知 metadata 地址；loopback/私网按需求保留。"""
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        candidates.append(address.ipv4_mapped)
    return any(
        candidate.is_link_local or candidate in _METADATA_ADDRESSES
        for candidate in candidates
    )


def _validate_probe_target(base_url: str) -> None:
    """探测前的 SSRF 防线：拒绝 link-local 与云 metadata。

    loopback 与 RFC1918 私网明确保留，供本地 Ollama/企业内网使用。
    """
    host = (urlsplit(base_url).hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BadRequestError("Base URL 缺少主机名", code="invalid_base_url")
    if host in _METADATA_HOSTNAMES:
        raise BadRequestError(
            "禁止探测云 metadata 地址", code="blocked_probe_target"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # 普通域名：保持可用；不在这里做 DNS 解析，避免额外等待与 DNS 重绑定竞态。
        return
    if _is_blocked_address(address):
        raise BadRequestError(
            "禁止探测 link-local 或云 metadata 地址", code="blocked_probe_target"
        )


def _probe_urls(base_url: str) -> list[str]:
    base = base_url.rstrip("/")
    if base.endswith(_MODELS_PATH):
        return [base]
    urls = [f"{base}{_MODELS_PATH}"]
    if not base.endswith("/v1"):
        urls.append(f"{base}/v1{_MODELS_PATH}")
    return urls


def _failure(message: str, started: float, status_code: int | None = None) -> ProviderTestResult:
    return ProviderTestResult(
        ok=False,
        message=message,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        status_code=status_code,
    )


async def probe_provider(base_url: str, api_key: str | None) -> ProviderTestResult:
    """对 OpenAI 兼容服务执行真实 HTTP 探测，总耗时不超过 5 秒。"""
    try:
        base = _normalize_base_url(base_url)
        _validate_probe_target(base)
    except BadRequestError as exc:
        return ProviderTestResult(
            ok=False,
            message=f"连接失败：{exc.message}",
            latency_ms=0.0,
        )

    started = time.perf_counter()
    deadline = started + PROVIDER_TEST_TIMEOUT_SECONDS
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = httpx.Timeout(PROVIDER_TEST_TIMEOUT_SECONDS)
    candidates = _probe_urls(base)
    last_status: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for index, url in enumerate(candidates):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return _failure(
                        "连接失败：请求超时（5 秒）",
                        started,
                        last_status,
                    )
                try:
                    # asyncio.timeout 是硬边界：连接、发送、读取响应体都受它约束。
                    async with asyncio.timeout(remaining):
                        response = await client.get(
                            url,
                            headers=headers,
                            timeout=httpx.Timeout(remaining),
                        )
                except (TimeoutError, httpx.TimeoutException):
                    return _failure("连接失败：请求超时（5 秒）", started, last_status)
                except httpx.HTTPError:
                    return _failure(
                        "连接失败：无法连接到目标服务（连接被拒绝或网络不可达）",
                        started,
                        last_status,
                    )

                last_status = response.status_code
                if 200 <= response.status_code < 300:
                    return ProviderTestResult(
                        ok=True,
                        message="连接成功",
                        latency_ms=round((time.perf_counter() - started) * 1000, 2),
                        status_code=response.status_code,
                    )
                if 300 <= response.status_code < 400:
                    return _failure(
                        f"连接失败：目标服务返回重定向（HTTP {response.status_code}），请填写最终 Base URL",
                        started,
                        response.status_code,
                    )
                if response.status_code in {401, 403}:
                    return _failure(
                        f"连接失败：认证被拒绝（HTTP {response.status_code}），请检查 API Key",
                        started,
                        response.status_code,
                    )
                if response.status_code == 404 and index < len(candidates) - 1:
                    continue
                return _failure(
                    f"连接失败：服务返回 HTTP {response.status_code}",
                    started,
                    response.status_code,
                )
    except Exception:  # 探测失败只影响本次结果，不能把异常细节/密钥写进日志
        logger.warning("provider connectivity probe failed", exc_info=False)
        return _failure("连接失败：连通性检查异常，请检查 Base URL", started, last_status)

    return _failure("连接失败：未找到可用的模型接口", started, last_status)


def _load_provider(session: Session, provider_id: str) -> ProviderConfig:
    ensure_no_nul(provider_id, "id")
    provider = session.get(ProviderConfig, provider_id)
    if provider is None:
        raise NotFoundError("Provider 不存在")
    return provider


def _apply_secret(provider: ProviderConfig, api_key: str | None) -> None:
    if api_key is None:
        return
    text = api_key.strip()
    if not text:
        # 留空表示不修改已保存的密钥。
        return
    provider.encrypted_secret = encrypt_secret(text)


def _record_test_audit(
    session: Session,
    context: AuthContext,
    target_id: str,
    result: ProviderTestResult,
) -> None:
    """记录连通性测试结果（成功/失败都写），只保留结果字段，不含密钥。"""
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="provider.tested",
        target_type="provider",
        target_id=target_id,
        actor_type=actor_type,
        actor_id=actor_id,
        after={
            "ok": result.ok,
            "status_code": result.status_code,
            "latency_ms": result.latency_ms,
        },
    )
    session.commit()


@router.get("")
def list_providers(
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    items = list(
        session.scalars(
            select(ProviderConfig).order_by(
                ProviderConfig.kind.asc(), ProviderConfig.name.asc()
            )
        )
    )
    return {
        "items": [_provider_payload(item) for item in items],
        "policy": _policy_payload().model_dump(),
    }


@router.get("/{provider_id}")
def get_provider(
    provider_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    return _provider_payload(_load_provider(session, provider_id))


@router.post("", status_code=201)
def create_provider(
    payload: ProviderCreate,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_no_nul(payload.name, "name")
    base_url = _normalize_base_url(payload.base_url) if payload.base_url else None
    provider = ProviderConfig(
        kind=payload.kind,
        name=payload.name,
        base_url=base_url,
        model=payload.model,
        dim=payload.dim,
        extra=payload.extra,
        enabled=payload.enabled,
    )
    _apply_secret(provider, payload.api_key)
    session.add(provider)
    try:
        session.flush()
        actor_type, actor_id = audit_actor(context)
        write_audit(
            session,
            action="provider.created",
            target_type="provider",
            target_id=provider.id,
            actor_type=actor_type,
            actor_id=actor_id,
            after={
                "kind": provider.kind,
                "name": provider.name,
                "base_url": provider.base_url,
                "model": provider.model,
                "dim": provider.dim,
                "enabled": provider.enabled,
                "has_secret": provider.encrypted_secret is not None,
            },
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("同类型下已存在同名 Provider") from exc
    return _provider_payload(provider)


@router.patch("/{provider_id}")
def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    provider = _load_provider(session, provider_id)
    before = _provider_payload(provider)
    fields = payload.model_fields_set

    if "kind" in fields and payload.kind is not None:
        provider.kind = payload.kind
    if "name" in fields and payload.name is not None:
        ensure_no_nul(payload.name, "name")
        provider.name = payload.name
    if "base_url" in fields:
        provider.base_url = (
            _normalize_base_url(payload.base_url) if payload.base_url else None
        )
    if "model" in fields:
        provider.model = payload.model
    if "dim" in fields:
        provider.dim = payload.dim
    if "extra" in fields and payload.extra is not None:
        provider.extra = payload.extra
    if "enabled" in fields and payload.enabled is not None:
        provider.enabled = payload.enabled
    if payload.clear_secret:
        provider.encrypted_secret = None
    else:
        _apply_secret(provider, payload.api_key)

    try:
        actor_type, actor_id = audit_actor(context)
        write_audit(
            session,
            action="provider.updated",
            target_type="provider",
            target_id=provider.id,
            actor_type=actor_type,
            actor_id=actor_id,
            before=before,
            after={
                **_provider_payload(provider),
                "secret_changed": "api_key" in fields or payload.clear_secret,
            },
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("同类型下已存在同名 Provider") from exc
    return _provider_payload(provider)


@router.delete("/{provider_id}", status_code=204)
def delete_provider(
    provider_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> Response:
    provider = _load_provider(session, provider_id)
    before = _provider_payload(provider)
    session.delete(provider)
    actor_type, actor_id = audit_actor(context)
    write_audit(
        session,
        action="provider.deleted",
        target_type="provider",
        target_id=provider_id,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
    )
    session.commit()
    return Response(status_code=204)


@router.post("/test")
async def test_provider_draft(
    payload: ProviderTestRequest,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> ProviderTestResult:
    """未保存表单的连通性测试；复用已存密钥时强制使用已保存的 Base URL。"""
    stored: ProviderConfig | None = None
    if payload.provider_id:
        stored = _load_provider(session, payload.provider_id)
    target_id = stored.id if stored is not None else "draft"

    api_key = (payload.api_key or "").strip()
    if not api_key and stored is not None and stored.encrypted_secret is not None:
        try:
            api_key = decrypt_secret(stored.encrypted_secret)
        except Exception:  # 密文损坏/密钥轮换
            result = ProviderTestResult(
                ok=False,
                message="连接失败：已保存的密钥无法解密，请重新填写",
                latency_ms=0.0,
            )
            _record_test_audit(session, context, target_id, result)
            return result
        # 安全边界：已存密钥只能发往它绑定的 Base URL，忽略请求里的覆盖值。
        base_url = stored.base_url
        if not base_url:
            result = ProviderTestResult(
                ok=False,
                message="连接失败：Provider 未配置 Base URL",
                latency_ms=0.0,
            )
            _record_test_audit(session, context, target_id, result)
            return result
    else:
        base_url = payload.base_url or (
            stored.base_url if stored is not None else None
        )
        if not base_url:
            result = ProviderTestResult(
                ok=False,
                message="连接失败：请先填写 Base URL",
                latency_ms=0.0,
            )
            _record_test_audit(session, context, target_id, result)
            return result

    result = await probe_provider(base_url, api_key or None)
    _record_test_audit(session, context, target_id, result)
    return result


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: str,
    context: AuthContext = Depends(require_scope("admin")),
    session: Session = Depends(get_db),
) -> ProviderTestResult:
    provider = _load_provider(session, provider_id)
    if not provider.base_url:
        result = ProviderTestResult(
            ok=False,
            message="连接失败：Provider 未配置 Base URL",
            latency_ms=0.0,
        )
        _record_test_audit(session, context, provider.id, result)
        return result
    api_key: str | None = None
    if provider.encrypted_secret is not None:
        try:
            api_key = decrypt_secret(provider.encrypted_secret)
        except Exception:
            result = ProviderTestResult(
                ok=False,
                message="连接失败：已保存的密钥无法解密，请重新填写",
                latency_ms=0.0,
            )
            _record_test_audit(session, context, provider.id, result)
            return result
    result = await probe_provider(provider.base_url, api_key)
    _record_test_audit(session, context, provider.id, result)
    return result


__all__ = ["probe_provider", "router"]
