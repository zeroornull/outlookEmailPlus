"""Telegram formatting/sending and mailbox fetches for queued notifications."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List

import requests

from outlook_web.services import notification_dispatch
from outlook_web.services.providers import (
    get_imap_folder_candidates,
    infer_provider_from_email,
)
from outlook_web.services.notification_transport import NotificationTransportError, retry_after_seconds
from outlook_web.services.imap_generic import decode_header_value

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

MAX_TELEGRAM_LENGTH = 4096
TELEGRAM_TEXT_LIMIT = 3500
MAX_PREVIEW_LENGTH = 200
MAX_EMAILS_PER_FETCH = 50


def _quote_imap_folder_name(folder_name: str) -> list[str]:
    name = (folder_name or "").strip()
    if not name:
        return []
    if name.startswith('"') and name.endswith('"'):
        return [name]
    if " " in name:
        return [name, f'"{name}"']
    return [name]


def _escape_html(text: str) -> str:
    """转义 Telegram HTML 模式必须转义的三种字符：& < >"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html_to_plain(html_str: str) -> str:
    """将 HTML 正文提取为纯文本（strip tags），合并多余空白。"""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _format_telegram_body(text: str) -> str:
    """保留换行，去掉零宽字符和行内多余空白。"""
    if not text:
        return ""
    lines = []
    for line in str(text).splitlines():
        line = re.sub(r"[\u200b-\u200f\ufeff\u034f\u00a0\u3000\u00ad]", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _format_sender_line(sender: str) -> str:
    raw = (sender or "").strip()
    if not raw:
        return ""
    from email.utils import parseaddr

    name, address = parseaddr(raw)
    name = (name or "").strip().strip('"')
    address = (address or "").strip()
    if name and address and name.casefold() != address.casefold():
        return f"{_escape_html(name)}  &lt;{_escape_html(address)}&gt;"
    return _escape_html(address or name or raw)


def _format_received_at(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 19 and text[10] == "T":
        return text[:10] + " " + text[11:]
    return text


def _verification_code(email: dict) -> str:
    from outlook_web.services.verification_code_extraction import smart_extract_verification_code

    source = "\n".join(
        part
        for part in (
            str(email.get("subject") or ""),
            str(email.get("content") or ""),
            str(email.get("preview") or ""),
        )
        if part
    )
    return smart_extract_verification_code(source) or ""


def _build_telegram_message(account_email: str, email: dict) -> str:
    """按 cloud-mail 的结构构造 Telegram HTML：主题作标题，正文独立成段。"""
    subject = _escape_html((email.get("subject") or "").strip() or "无主题")
    sender_line = _format_sender_line(str(email.get("sender") or ""))
    recipient = _escape_html(account_email or "")
    received_at = _escape_html(_format_received_at(str(email.get("received_at") or "")))
    code = _verification_code(email)
    body = _format_telegram_body(email.get("content") or email.get("preview") or "")
    if len(body) > TELEGRAM_TEXT_LIMIT:
        body = body[: TELEGRAM_TEXT_LIMIT - 3].rstrip() + "..."

    lines = [f"<b>{subject}</b>", ""]
    if sender_line:
        lines.append(f"发件人\u200b：{sender_line}")
    if recipient:
        lines.append(f"收件人\u200b：{recipient}")
    if received_at:
        lines.append(f"时间\u200b：{received_at}")
    if body:
        lines.extend(["", _escape_html(body)])
    if code:
        lines.extend(["", f"验证码：<code>{_escape_html(code)}</code>"])

    msg = "\n".join(lines)
    if len(msg) > MAX_TELEGRAM_LENGTH:
        msg = msg[: MAX_TELEGRAM_LENGTH - 3] + "..."
    return msg


def _telegram_reply_markup(account_email: str, email: dict, chat_id: str) -> dict | None:
    """首行查看单封邮件，验证码另行复制；群聊使用普通链接。"""
    from outlook_web.services.telegram_preview import create_telegram_preview_url

    rows = []
    preview_url = create_telegram_preview_url(account_email, email)
    if preview_url:
        target = "web_app" if str(chat_id).strip().isdigit() else "url"
        button = {"text": "查看邮件", target: {"url": preview_url} if target == "web_app" else preview_url}
        rows.append([button])
    code = _verification_code(email)
    if code:
        rows.append([{"text": f"复制验证码 {code}", "copy_text": {"text": code}}])
    return {"inline_keyboard": rows} if rows else None


def _send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    """调用 Telegram sendMessage API。超时 10 秒，失败返回 False。
    自动读取系统 telegram_proxy_url 设置并透传给 requests。
    查看或复制按钮被接口拒绝时，去掉按钮再发一次正文。
    """
    from outlook_web.repositories import settings as settings_repo
    from outlook_web.services.graph import build_proxies

    proxy_url = settings_repo.get_telegram_proxy_url()
    proxies = build_proxies(proxy_url) if proxy_url else None
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def post(markup: dict | None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            payload["reply_markup"] = markup
        return requests.post(url, json=payload, timeout=10, proxies=proxies)

    try:
        resp = post(reply_markup)
        if not resp.ok and reply_markup and resp.status_code == 400:
            logger.warning("[telegram_push] inline buttons rejected, retrying without them")
            resp = post(None)
        if resp.status_code == 429:
            try:
                retry_after = retry_after_seconds(resp.json().get("parameters", {}).get("retry_after"))
            except (ValueError, AttributeError, TypeError):
                retry_after = None
            raise NotificationTransportError("Telegram rate limited", code="TELEGRAM_RATE_LIMITED", retry_after=retry_after if retry_after is not None else retry_after_seconds(resp.headers.get("Retry-After")), status=429)
        if not resp.ok:
            logger.warning("[telegram_push] send HTTP %s: %s", resp.status_code, resp.text[:200])
        return resp.ok
    except NotificationTransportError:
        raise
    except Exception as e:
        logger.warning("[telegram_push] send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# 邮件拉取（IMAP / Graph）
# ---------------------------------------------------------------------------


def _resolve_imap_folder(account: dict, folder: str) -> list[str]:
    provider = str(account.get("provider") or "").strip().lower()
    if provider in {"", "imap"}:
        provider = infer_provider_from_email(str(account.get("email") or "")) or provider
    candidates = get_imap_folder_candidates(provider, folder)
    resolved: list[str] = []
    for candidate in candidates:
        resolved.extend(_quote_imap_folder_name(candidate))
    return resolved or ["INBOX"]


def _resolve_listed_imap_junk_folders(conn, account: dict) -> list[str]:
    """Only a complete successful LIST can establish optional junk absence."""
    status, data = conn.list()
    if status != "OK" or not isinstance(data, (list, tuple)):
        raise NotificationTransportError("IMAP folder listing failed", code="IMAP_LIST_FAILED")
    junk_names = {name.strip('"').casefold() for name in _resolve_imap_folder(account, "junkemail")}
    junk_names.update({"junk", "junk email", "junkemail", "spam", "bulk mail", "垃圾邮件"})
    special_use, named = [], []
    for item in data:
        if item is None:
            continue
        # imaplib represents literal mailbox names as (metadata, literal).
        literal = None
        if isinstance(item, tuple) and len(item) == 2:
            item, literal = item
        if not isinstance(item, bytes):
            raise NotificationTransportError("Invalid IMAP folder listing", code="IMAP_LIST_FAILED")
        match = re.fullmatch(rb'\(([^)]*)\)\s+(NIL|"(?:\\.|[^"\\])*"|[^\s]+)\s+("(?:\\.|[^"\\])*"|[^\s]+)', item)
        if not match:
            raise NotificationTransportError("Invalid IMAP folder listing", code="IMAP_LIST_FAILED")

        def unquote(value: bytes) -> str:
            if value.startswith(b'"') and value.endswith(b'"'):
                value = re.sub(rb'\\(.)', rb'\1', value[1:-1])
            return value.decode("utf-8", errors="replace")

        flags = match.group(1).lower().split()
        delimiter = "" if match.group(2).upper() == b"NIL" else unquote(match.group(2))
        if literal is not None:
            if not isinstance(literal, bytes) or match.group(3) != b"{" + str(len(literal)).encode("ascii") + b"}":
                raise NotificationTransportError("Invalid IMAP folder literal", code="IMAP_LIST_FAILED")
            name = literal.decode("utf-8", errors="replace")
        else:
            name = unquote(match.group(3))
        suffix = name.rsplit(delimiter, 1)[-1] if delimiter else name
        target = _quote_imap_folder_name(name)[-1]
        if b"\\junk" in flags:
            special_use.append(target)
        elif name.casefold() in junk_names or suffix.casefold() in junk_names:
            named.append(target)
    return list(dict.fromkeys(special_use + named))


def _should_fetch_account_via_graph(account: dict) -> bool:
    account_type = str(account.get("account_type") or "").strip().lower()
    if account_type:
        return account_type == "outlook"
    return str(account.get("provider") or "").strip().lower() == "outlook"



def _fetch_new_emails_imap(account: dict, since: str, folder: str = "inbox") -> List[dict]:
    """Drain matching UIDs in sequential batches using server arrival time."""
    import email as email_lib
    import imaplib
    from outlook_web.security.crypto import decrypt_data

    since = notification_dispatch._extract_message_timestamp(since)
    since_date = datetime.fromisoformat(since).strftime("%d-%b-%Y")
    conn = None
    results = []
    try:
        conn = imaplib.IMAP4_SSL(account.get("imap_host", ""), int(account.get("imap_port", 993)), timeout=15)
        conn.login(account.get("email", ""), decrypt_data(account.get("imap_password", "")))
        candidates = (_resolve_listed_imap_junk_folders(conn, account)
                      if folder == "junkemail" else _resolve_imap_folder(account, folder))
        if folder == "junkemail" and not candidates:
            return []
        for folder_name in candidates:
            status, _ = conn.select(folder_name, readonly=True)
            if status == "OK":
                break
            if folder == "junkemail" or status != "NO":
                raise NotificationTransportError("IMAP folder selection failed", code="IMAP_SELECT_FAILED")
        else:
            raise NotificationTransportError("IMAP folder selection failed", code="IMAP_SELECT_FAILED")
        _, validity_data = conn.response("UIDVALIDITY")
        validity = validity_data[0].decode("ascii") if validity_data and validity_data[0] else ""
        status, data = conn.uid("search", None, f'(SINCE "{since_date}")')
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise NotificationTransportError("IMAP UID search failed", code="IMAP_SEARCH_FAILED")
        uids = sorted(data[0].split(), key=int)
        for offset in range(0, len(uids), MAX_EMAILS_PER_FETCH):
            batch = uids[offset:offset + MAX_EMAILS_PER_FETCH]
            status, date_data = conn.uid("fetch", b",".join(batch), "(UID INTERNALDATE)")
            if status != "OK":
                raise NotificationTransportError("IMAP date fetch failed", code="IMAP_FETCH_FAILED")
            arrivals = {}
            for item in date_data or []:
                header = item[0] if isinstance(item, tuple) else item
                if not isinstance(header, bytes):
                    continue
                uid = re.search(rb"\bUID (\d+)", header, re.I)
                date = re.search(rb'INTERNALDATE "([^"]+)"', header, re.I)
                if uid and date:
                    arrivals[uid.group(1)] = notification_dispatch._extract_message_timestamp(datetime.strptime(date.group(1).decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z").isoformat())
            if any(uid not in arrivals for uid in batch):
                raise NotificationTransportError("Incomplete IMAP date batch", code="IMAP_FETCH_INCOMPLETE")
            selected = [uid for uid in batch if arrivals[uid] >= since]
            if not selected:
                continue
            status, body_data = conn.uid("fetch", b",".join(selected), "(UID BODY.PEEK[])")
            if status != "OK":
                raise NotificationTransportError("IMAP body fetch failed", code="IMAP_FETCH_FAILED")
            fetched = set()
            for item in body_data or []:
                if not isinstance(item, tuple) or len(item) < 2 or not isinstance(item[0], bytes):
                    continue
                match = re.search(rb"\bUID (\d+)", item[0], re.I)
                if not match or match.group(1) not in selected or not isinstance(item[1], bytes):
                    continue
                uid = match.group(1)
                fetched.add(uid)
                msg = email_lib.message_from_bytes(item[1])
                subject = decode_header_value(msg.get("Subject", ""))
                body, html_content = "", ""
                for part in msg.walk():
                    if part.get_content_disposition() == "attachment" or part.get_filename():
                        continue
                    content_type = part.get_content_type()
                    if content_type not in {"text/plain", "text/html"}:
                        continue
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    try:
                        decoded = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    except (LookupError, ValueError):
                        decoded = payload.decode("utf-8", errors="replace")
                    if content_type == "text/plain" and not body:
                        body = decoded
                    elif content_type == "text/html" and not html_content:
                        html_content = decoded
                if not body and html_content:
                    body = _html_to_plain(html_content)
                message_id = str(msg.get("Message-ID") or "").strip()
                if not message_id:
                    if not validity:
                        raise NotificationTransportError("IMAP UIDVALIDITY missing", code="IMAP_UIDVALIDITY_MISSING")
                    message_id = f"imap:{account.get('email', '')}:{folder}:{validity}:{uid.decode('ascii')}"
                results.append({"message_id": message_id, "subject": subject, "sender": msg.get("From", ""),
                                "received_at": arrivals[uid], "preview": body[:MAX_PREVIEW_LENGTH],
                                "content": body, "html_content": html_content, "folder": folder})
            if fetched != set(selected):
                raise NotificationTransportError("Incomplete IMAP body batch", code="IMAP_FETCH_INCOMPLETE")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return sorted(results, key=lambda item: (item["received_at"], item["message_id"]))


def _fetch_new_emails_graph(account: dict, since: str, folder: str = "inbox") -> List[dict]:
    """Drain Graph pages; incomplete fetches must retain the source cursor."""
    from urllib.parse import urlsplit
    from outlook_web.security.crypto import decrypt_data
    from outlook_web.repositories import accounts as accounts_repo
    from outlook_web.services.graph import build_proxies, get_access_token_graph_result, invalidate_graph_access_token

    client_id = account.get("client_id", "")
    refresh_token = decrypt_data(account.get("refresh_token", ""))
    proxy_url = account.get("proxy_url", "") or ""
    token_result = get_access_token_graph_result(client_id, refresh_token, proxy_url, account_id=int(account["id"]))
    if not token_result.get("success") or not token_result.get("access_token"):
        error = token_result.get("error") or {}
        code = error.get("code", "GRAPH_AUTH_FAILED") if isinstance(error, dict) else str(error)
        status = error.get("status", 401) if isinstance(error, dict) else 401
        raise NotificationTransportError("Graph token acquisition failed", code=code, retry_after=token_result.get("retry_after"), status=status)
    rotated = token_result.get("new_refresh_token") or token_result.get("refresh_token")
    if rotated and rotated != refresh_token:
        account["refresh_token"] = rotated
        refresh_token = rotated
    folder_name = (folder or "inbox").strip().lower()
    if folder_name not in notification_dispatch.ACCOUNT_INCLUDED_FOLDERS:
        raise ValueError("Unsupported notification Graph folder")
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder_name}/messages"
    params = {"$filter": f"receivedDateTime ge {notification_dispatch._extract_message_timestamp(since)}Z",
              "$top": MAX_EMAILS_PER_FETCH, "$select": "id,subject,from,receivedDateTime,bodyPreview,internetMessageId,body",
              "$orderby": "receivedDateTime asc"}
    headers = {"Authorization": f"Bearer {token_result['access_token']}"}
    proxies = build_proxies(proxy_url)
    results, seen_pages = [], set()
    while url:
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname != "graph.microsoft.com" or target.port not in {None, 443} or target.username or target.password:
            raise NotificationTransportError("Unsafe Graph pagination URL", code="GRAPH_INVALID_NEXT_LINK")
        if url in seen_pages:
            raise NotificationTransportError("Repeated Graph pagination URL", code="GRAPH_INVALID_NEXT_LINK")
        seen_pages.add(url)
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15, proxies=proxies)
        except requests.RequestException as exc:
            raise NotificationTransportError("Graph request failed", code="GRAPH_FETCH_FAILED") from exc
        if not resp.ok:
            if resp.status_code == 401:
                invalidate_graph_access_token(client_id, refresh_token, proxy_url, account_id=int(account["id"]))
            raise NotificationTransportError("Graph message fetch failed", code="GRAPH_RATE_LIMITED" if resp.status_code == 429 else "GRAPH_FETCH_FAILED", retry_after=retry_after_seconds(resp.headers.get("Retry-After")), status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise NotificationTransportError("Invalid Graph message response", code="GRAPH_INVALID_RESPONSE") from exc
        if not isinstance(data, dict) or not isinstance(data.get("value"), list):
            raise NotificationTransportError("Invalid Graph message response", code="GRAPH_INVALID_RESPONSE")
        for item in data["value"]:
            sender_info = (item.get("from") or {}).get("emailAddress") or {}
            name, address = (sender_info.get("name") or "").strip(), (sender_info.get("address") or "").strip()
            sender = f"{name} <{address}>" if name and address and name.casefold() != address.casefold() else address or name
            try:
                received_at = notification_dispatch._extract_message_timestamp(item.get("receivedDateTime"))
            except (TypeError, ValueError) as exc:
                raise NotificationTransportError("Invalid Graph message timestamp", code="GRAPH_INVALID_RESPONSE") from exc
            if not received_at:
                raise NotificationTransportError("Graph message timestamp missing", code="GRAPH_INVALID_RESPONSE")
            body_info = item.get("body") or {}
            html_content = (body_info.get("content") or "") if str(body_info.get("contentType") or "").lower() == "html" else ""
            body = _html_to_plain(html_content) if html_content else body_info.get("content", "") or ""
            results.append({"message_id": item.get("internetMessageId") or item.get("id", ""),
                            "subject": item.get("subject", ""), "sender": sender, "received_at": received_at,
                            "preview": (item.get("bodyPreview") or "")[:MAX_PREVIEW_LENGTH],
                            "content": body, "html_content": html_content, "folder": folder_name})
        url = data.get("@odata.nextLink")
        if url is not None and not isinstance(url, str):
            raise NotificationTransportError("Invalid Graph pagination URL", code="GRAPH_INVALID_NEXT_LINK")
        params = None
    return results


