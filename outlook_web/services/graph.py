from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time

from typing import Any, Dict, List, Optional

import requests

from outlook_web.errors import build_error_payload
from outlook_web.repositories import settings as settings_repo
from outlook_web.repositories import accounts as accounts_repo
from outlook_web.services.http import get_response_details
from outlook_web.services.oauth_refresh import post_refresh_token
from outlook_web.services.notification_transport import retry_after_seconds

# Token 端点
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TOKEN_URL_GRAPH = TOKEN_URL_TEMPLATE.format(tenant="common")
DEFAULT_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_MAIL_READ_SCOPES = ("Mail.Read", "Mail.ReadWrite")

# Graph API 返回 401 时表示账号授权失效（与 token endpoint 失败不同）
GRAPH_AUTH_EXPIRED_STATUS = 401

GRAPH_TOKEN_CACHE_MAX_ENTRIES = 1024
GRAPH_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0
GRAPH_TOKEN_DEFAULT_RETRY_SECONDS = 60.0


@dataclass
class _GraphTokenEntry:
    result: Dict[str, Any]
    expires_at: float


_graph_token_cache: OrderedDict[str, _GraphTokenEntry] = OrderedDict()
_graph_token_inflight: Dict[str, Future] = {}
_graph_token_lock = threading.Lock()


def _graph_token_key(client_id, refresh_token, proxy_url, client_secret, account_id=None) -> str:
    credentials = json.dumps([client_id, refresh_token, proxy_url or "", client_secret or "", account_id], ensure_ascii=False)
    return hashlib.sha256(credentials.encode("utf-8")).hexdigest()


def _prune_graph_token_cache(now: float) -> None:
    for key, entry in list(_graph_token_cache.items()):
        if entry.expires_at <= now:
            del _graph_token_cache[key]
    while len(_graph_token_cache) > GRAPH_TOKEN_CACHE_MAX_ENTRIES:
        _, oldest = _graph_token_cache.popitem(last=False)
        for alias, entry in list(_graph_token_cache.items()):
            if entry is oldest:
                del _graph_token_cache[alias]


def clear_graph_token_cache() -> None:
    """Clear cached tokens/cooldowns; in-flight requests cannot repopulate them."""
    with _graph_token_lock:
        _graph_token_cache.clear()
        _graph_token_inflight.clear()


def invalidate_graph_access_token(client_id: str, refresh_token: str, proxy_url: str = None, *, account_id: int | None = None) -> None:
    """Invalidate a rejected access token and all of its rotated-token aliases."""
    client_secret = settings_repo.get_oauth_client_secret_for_client(client_id)
    key = _graph_token_key(client_id, refresh_token, proxy_url, client_secret, account_id)
    with _graph_token_lock:
        entry = _graph_token_cache.get(key)
        if entry is not None:
            for alias, candidate in list(_graph_token_cache.items()):
                if candidate is entry:
                    del _graph_token_cache[alias]
        _graph_token_inflight.pop(key, None)


def _graph_token_lifetime(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 0.0
    try:
        lifetime = float(value)
    except (ValueError, OverflowError):
        return 0.0
    if not math.isfinite(lifetime) or lifetime <= 0:
        return 0.0
    return max(0.0, lifetime - min(GRAPH_TOKEN_EXPIRY_MARGIN_SECONDS, lifetime * 0.1))


def build_proxies(proxy_url: str) -> Optional[Dict[str, str]]:
    """构建 requests 的 proxies 参数"""
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_token_url(tenant: str | None = None) -> str:
    """按 tenant 生成 Microsoft OAuth token endpoint。"""
    normalized_tenant = (tenant or "common").strip() or "common"
    return TOKEN_URL_TEMPLATE.format(tenant=normalized_tenant)


def get_access_token_graph_result(client_id: str, refresh_token: str, proxy_url: str = None, *, account_id: int | None = None) -> Dict[str, Any]:
    """Get a token with bounded expiry, same-key single flight and rate cooldown."""
    try:
        client_secret = settings_repo.get_oauth_client_secret_for_client(client_id)
        key = _graph_token_key(client_id, refresh_token, proxy_url, client_secret, account_id)
    except Exception as exc:
        return _graph_token_exception(exc)

    with _graph_token_lock:
        now = time.monotonic()
        _prune_graph_token_cache(now)
        entry = _graph_token_cache.get(key)
        if entry is not None:
            _graph_token_cache.move_to_end(key)
            result = deepcopy(entry.result)
            if not result.get("success"):
                result["retry_after"] = max(0.0, entry.expires_at - now)
            return result
        future = _graph_token_inflight.get(key)
        owner = future is None
        if owner:
            future = Future()
            _graph_token_inflight[key] = future

    if not owner:
        return deepcopy(future.result())

    started_at = time.monotonic()
    try:
        result = _refresh_graph_access_token(client_id, refresh_token, proxy_url, client_secret)
        if result.get("success") and account_id is not None:
            try:
                persisted = accounts_repo.persist_token_refresh(account_id, client_id, refresh_token, result.get("new_refresh_token"))
                result["refresh_token"] = persisted
                result["new_refresh_token"] = persisted
            except accounts_repo.TokenPersistenceError:
                result = {"success": False, "retry_after": None, "error": build_error_payload(
                    "TOKEN_PERSISTENCE_FAILED", "保存刷新令牌失败", "TokenPersistenceError", 500, "Token persistence failed")}
        lifetime = _graph_token_lifetime(result.get("expires_in")) if result.get("success") else result.get("retry_after")
        expires_at = started_at + lifetime if result.get("success") and lifetime else time.monotonic() + (lifetime or 0)
        with _graph_token_lock:
            # Explicit invalidation/clearing while the POST ran also discards its cache write.
            if _graph_token_inflight.get(key) is future:
                if expires_at > time.monotonic():
                    entry = _GraphTokenEntry(deepcopy(result), expires_at)
                    _graph_token_cache[key] = entry
                    rotated = result.get("new_refresh_token")
                    if result.get("success") and isinstance(rotated, str) and rotated.strip():
                        alias = _graph_token_key(client_id, rotated, proxy_url, client_secret, account_id)
                        _graph_token_cache[alias] = entry
                    _prune_graph_token_cache(time.monotonic())
                del _graph_token_inflight[key]
        future.set_result(result)
        return deepcopy(result)
    except BaseException as exc:
        with _graph_token_lock:
            if _graph_token_inflight.get(key) is future:
                del _graph_token_inflight[key]
        future.set_exception(exc)
        raise


def _refresh_graph_access_token(client_id, refresh_token, proxy_url, client_secret) -> Dict[str, Any]:
    try:
        proxies = build_proxies(proxy_url)
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": DEFAULT_GRAPH_SCOPE,
        }
        # Use the same resolved secret as the cache key.
        if client_secret:
            data["client_secret"] = client_secret
        res = post_refresh_token(TOKEN_URL_GRAPH, data=data, timeout=30, proxies=proxies)

        if res.status_code != 200:
            details = get_response_details(res)
            if res.status_code == 400 and isinstance(details, dict) and details.get("error") == "invalid_request":
                description = details.get("error_description")
                codes = details.get("error_codes")
                has_permission_code = isinstance(codes, list) and any(code in (90023, "90023") for code in codes)
                if (
                    isinstance(description, str)
                    and (has_permission_code or "aadsts90023:" in description.lower())
                    and "no applicable permissions" in description.lower()
                ):
                    # .default may omit historical dynamic grants; Microsoft still validates Mail.Read consent.
                    mail_data = data.copy()
                    mail_data["scope"] = "https://graph.microsoft.com/Mail.Read"
                    res = post_refresh_token(TOKEN_URL_GRAPH, data=mail_data, timeout=30, proxies=proxies)
                    if res.status_code != 200:
                        details = get_response_details(res)

        if res.status_code != 200:
            retry_after = retry_after_seconds(res.headers.get("Retry-After"))
            if res.status_code == 429 and retry_after is None:
                retry_after = GRAPH_TOKEN_DEFAULT_RETRY_SECONDS
            return {
                "success": False,
                "retry_after": retry_after,
                "error": build_error_payload(
                    "GRAPH_TOKEN_FAILED",
                    "获取访问令牌失败",
                    "GraphAPIError",
                    res.status_code,
                    details,
                ),
            }

        payload = res.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return {
                "success": False,
                "retry_after": None,
                "error": build_error_payload(
                    "GRAPH_TOKEN_MISSING",
                    "获取访问令牌失败",
                    "GraphAPIError",
                    res.status_code,
                    payload,
                ),
            }

        # 根据 Microsoft Learn 文档：refresh token 可能会在每次使用时"自我替换"，应保存新的 refresh_token（如有）。
        new_refresh_token = payload.get("refresh_token")
        return {
            "success": True,
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "new_refresh_token": new_refresh_token,
            "scope": payload.get("scope", ""),
            "expires_in": payload.get("expires_in"),
        }
    except Exception as exc:
        return _graph_token_exception(exc)


def _graph_token_exception(exc: Exception) -> Dict[str, Any]:
    return {
        "success": False,
        "retry_after": None,
        "error": build_error_payload(
            "GRAPH_TOKEN_EXCEPTION",
            "获取访问令牌失败",
            type(exc).__name__,
            500,
            str(exc),
        ),
    }


def has_mail_read_permission(scope: Any) -> bool:
    scope_str = str(scope or "")
    return any(mail_scope in scope_str for mail_scope in GRAPH_MAIL_READ_SCOPES)


def get_access_token_graph(client_id: str, refresh_token: str, proxy_url: str = None, *, account_id: int | None = None) -> Optional[str]:
    """获取 Graph API access_token"""
    result = get_access_token_graph_result(client_id, refresh_token, proxy_url, account_id=account_id)
    if result.get("success"):
        return result.get("access_token")
    return None


def get_emails_graph(
    client_id: str,
    refresh_token: str,
    folder: str = "inbox",
    skip: int = 0,
    top: int = 20,
    proxy_url: str = None,
    *,
    account_id: int | None = None,
) -> Dict[str, Any]:
    """使用 Graph API 获取邮件列表（支持分页和文件夹选择）"""
    token_result = get_access_token_graph_result(client_id, refresh_token, proxy_url, account_id=account_id)
    if not token_result.get("success"):
        return {"success": False, "error": token_result.get("error")}

    access_token = token_result.get("access_token")
    scope = token_result.get("scope", "")
    if not has_mail_read_permission(scope):
        return {
            "success": False,
            "auth_expired": True,
            "no_mail_permission": True,
            "error": build_error_payload(
                "NO_MAIL_PERMISSION",
                "此账号未授予邮件读取权限 (scope 中不含 Mail.Read)",
                "PermissionError",
                403,
                f"scope={scope}",
            ),
        }

    try:
        folder_map = {
            "inbox": "inbox",
            "junkemail": "junkemail",
            "deleteditems": "deleteditems",
            "trash": "deleteditems",
        }
        folder_name = folder_map.get((folder or "").lower(), "inbox")

        url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_name}/messages"
        params = {
            "$top": top,
            "$skip": skip,
            "$select": "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview",
            "$orderby": "receivedDateTime desc",
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": "outlook.body-content-type='text'",
        }

        proxies = build_proxies(proxy_url)
        res = requests.get(url, headers=headers, params=params, timeout=30, proxies=proxies)

        if res.status_code != 200:
            if res.status_code == GRAPH_AUTH_EXPIRED_STATUS:
                invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=account_id)
            details = get_response_details(res)
            return {
                "success": False,
                "auth_expired": res.status_code == GRAPH_AUTH_EXPIRED_STATUS,
                "error": build_error_payload(
                    "EMAIL_FETCH_FAILED",
                    "获取邮件失败，请检查账号配置",
                    "GraphAPIError",
                    res.status_code,
                    details,
                ),
            }

        return {
            "success": True,
            "emails": res.json().get("value", []),
            "new_refresh_token": token_result.get("refresh_token"),
        }
    except Exception as exc:
        return {
            "success": False,
            "error": build_error_payload(
                "EMAIL_FETCH_FAILED",
                "获取邮件失败，请检查账号配置",
                type(exc).__name__,
                500,
                str(exc),
            ),
        }


def get_email_detail_graph(
    client_id: str,
    refresh_token: str,
    message_id: str,
    proxy_url: str = None,
    *,
    account_id: int | None = None,
) -> Optional[Dict]:
    """使用 Graph API 获取邮件详情"""
    access_token = get_access_token_graph(client_id, refresh_token, proxy_url, account_id=account_id)
    if not access_token:
        return None

    try:
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        params = {
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,isRead,hasAttachments,body,bodyPreview"
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": "outlook.body-content-type='html'",
        }

        proxies = build_proxies(proxy_url)
        res = requests.get(url, headers=headers, params=params, timeout=30, proxies=proxies)

        if res.status_code != 200:
            if res.status_code == GRAPH_AUTH_EXPIRED_STATUS:
                invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=account_id)
            return None

        return res.json()
    except Exception:
        return None


def get_email_raw_graph(
    client_id: str,
    refresh_token: str,
    message_id: str,
    proxy_url: str = None,
    *,
    account_id: int | None = None,
) -> Optional[str]:
    """使用 Graph API 获取邮件 MIME RAW 内容。"""
    access_token = get_access_token_graph(client_id, refresh_token, proxy_url, account_id=account_id)
    if not access_token:
        return None

    try:
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}/$value"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        proxies = build_proxies(proxy_url)
        res = requests.get(url, headers=headers, timeout=30, proxies=proxies)

        if res.status_code != 200:
            if res.status_code == GRAPH_AUTH_EXPIRED_STATUS:
                invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=account_id)
            return None

        res.encoding = res.encoding or "utf-8"
        return res.text
    except Exception:
        return None


def test_refresh_token(client_id: str, refresh_token: str, proxy_url: str = None) -> tuple[bool, str | None]:
    """测试 refresh token 是否有效，返回 (是否成功, 错误信息)"""
    ok, err, _new_refresh_token = test_refresh_token_with_rotation(client_id, refresh_token, proxy_url)
    return ok, err


def test_refresh_token_with_rotation(
    client_id: str,
    refresh_token: str,
    proxy_url: str = None,
    *,
    tenant: str = "common",
    scope: str | None = None,
    max_retries: int = 3,
) -> tuple[bool, str | None, str | None]:
    """测试 refresh token 是否有效；如服务端返回新的 refresh_token，则一并返回（用于滚动更新）。
    支持指数退避重试，遇到 429 时优先读取 Retry-After 头。"""
    import time

    proxies = build_proxies(proxy_url)
    resolved_scope = (scope or "").strip()
    url = build_token_url(tenant)
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if resolved_scope:
        data["scope"] = resolved_scope
    client_secret = settings_repo.get_oauth_client_secret_for_client(client_id)
    if client_secret:
        data["client_secret"] = client_secret

    last_error_msg = None
    for attempt in range(max_retries + 1):
        try:
            res = post_refresh_token(url, data=data, timeout=15, proxies=proxies)

            if res.status_code == 200:
                try:
                    payload = res.json()
                except Exception:
                    payload = {}
                access_token = payload.get("access_token") if isinstance(payload, dict) else None
                if not isinstance(access_token, str) or not access_token.strip():
                    return False, "获取访问令牌失败：响应缺少 access_token", None
                new_refresh_token = payload.get("refresh_token")
                return True, None, new_refresh_token

            # 429 限流：读取 Retry-After 并退避
            if res.status_code == 429:
                retry_after = None
                try:
                    retry_after = int(res.headers.get("Retry-After", 0))
                except Exception:
                    retry_after = None
                wait = retry_after if retry_after else (2**attempt)
                last_error_msg = f"请求被限流 (429)，{wait}s 后重试"
                if attempt < max_retries:
                    time.sleep(wait)
                    continue

            try:
                error_data = res.json()
            except Exception:
                error_data = {}
            error_msg = None
            if isinstance(error_data, dict):
                error_msg = error_data.get("error_description") or error_data.get("error")
            if not error_msg:
                details = get_response_details(res)
                error_msg = str(details)[:800] if details is not None else "未知错误"
            last_error_msg = str(error_msg)
            # 非 429 的明确错误响应（如 400/401/403）不需要重试，直接返回
            return False, last_error_msg, None
        except Exception as e:
            last_error_msg = f"请求异常: {str(e)}"
            if attempt < max_retries:
                time.sleep(2**attempt)
                continue
            return False, last_error_msg, None

    return False, last_error_msg or "请求失败", None


def delete_emails_graph(
    client_id: str,
    refresh_token: str,
    message_ids: List[str],
    proxy_url: str = None,
    *,
    account_id: int | None = None,
) -> Dict[str, Any]:
    """通过 Graph API 批量删除邮件（永久删除）"""
    token_result = get_access_token_graph_result(client_id, refresh_token, proxy_url, account_id=account_id)
    if not token_result.get("success"):
        return {"success": False, "error": token_result.get("error")}

    access_token = token_result.get("access_token")
    if not access_token:
        return {
            "success": False,
            "error": build_error_payload(
                "GRAPH_TOKEN_FAILED",
                "获取访问令牌失败",
                "GraphAPIError",
                500,
                "empty_access_token",
            ),
        }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Graph API batch 请求每次最多 20
    batch_size = 20
    success_count = 0
    failed_count = 0
    errors: List[str] = []

    for i in range(0, len(message_ids), batch_size):
        batch = message_ids[i : i + batch_size]

        batch_requests = []
        for idx, msg_id in enumerate(batch):
            batch_requests.append({"id": str(idx), "method": "DELETE", "url": f"/me/messages/{msg_id}"})

        try:
            proxies = build_proxies(proxy_url)
            response = requests.post(
                "https://graph.microsoft.com/v1.0/$batch",
                headers=headers,
                json={"requests": batch_requests},
                timeout=30,
                proxies=proxies,
            )

            if response.status_code == 200:
                results = response.json().get("responses", [])
                for res in results:
                    if res.get("status") in [200, 204]:
                        success_count += 1
                    else:
                        if res.get("status") == GRAPH_AUTH_EXPIRED_STATUS:
                            invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=account_id)
                        failed_count += 1
                        try:
                            errors.append(f"Msg ID: {batch[int(res['id'])]}, Status: {res.get('status')}")
                        except Exception:
                            errors.append(f"Status: {res.get('status')}")
            else:
                if response.status_code == GRAPH_AUTH_EXPIRED_STATUS:
                    invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=account_id)
                failed_count += len(batch)
                errors.append(f"Batch request failed: {response.text}")
        except Exception as e:
            failed_count += len(batch)
            errors.append(f"Network error: {str(e)}")

    result: Dict[str, Any] = {
        "success": success_count > 0,
        "partial_success": success_count > 0 and failed_count > 0,
        "success_count": success_count,
        "failed_count": failed_count,
        "errors": errors,
    }

    if not result["success"]:
        result["error"] = build_error_payload(
            "EMAIL_DELETE_FAILED",
            "删除邮件失败",
            "GraphAPIError",
            502,
            {"failed_count": failed_count, "errors": errors[:10]},
        )

    return result
