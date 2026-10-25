from __future__ import annotations

import json
import os
import re
import textwrap
import time
from typing import Any

import requests

from src.utils.helpers import short_text, slugify


FEISHU_API_BASE_URL = "https://open.feishu.cn/open-apis"
FEISHU_LEADING_MENTION_PATTERN = re.compile(r"^\s*(?:@[^\s/]+\s*)+")


def build_feishu_thread_peer_id(chat_id: str, thread_id: str) -> str:
    return f"{chat_id.strip()}:topic:{thread_id.strip()}".lower()


def build_issue_agent_id(config: dict[str, Any], repo_full_name: str, issue_number: int) -> str:
    prefix = slugify(config.get("issue_session_prefix", "gh"), limit=12)
    repo_slug = slugify(repo_full_name.replace("/", "-"), limit=48)
    if prefix:
        return f"{prefix}-{repo_slug}-issue-{issue_number}"
    return f"{repo_slug}-issue-{issue_number}"


def build_feishu_thread_session_key(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    chat_id: str,
    thread_id: str,
) -> str:
    agent_id = build_issue_agent_id(config, repo_full_name, issue_number).lower()
    peer_id = build_feishu_thread_peer_id(chat_id, thread_id)
    return f"agent:{agent_id}:feishu:thread:{peer_id}"


def is_feishu_route_session_key(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized.startswith("agent:") and (
        ":feishu:group:" in normalized or ":feishu:thread:" in normalized
    )


def resolve_feishu_runtime_settings(config: dict[str, Any]) -> dict[str, Any]:
    app_id = str(config.get("feishu_app_id") or os.getenv("FEISHU_APP_ID", "")).strip()
    app_secret = str(config.get("feishu_app_secret") or os.getenv("FEISHU_APP_SECRET", "")).strip()
    configured_chat_ids = [
        str(item).strip()
        for item in (config.get("feishu_handoff_chat_ids") or [])
        if str(item).strip()
    ]
    if config.get("feishu_handoff_chat_id"):
        configured_chat_ids.insert(0, str(config["feishu_handoff_chat_id"]).strip())
    chat_ids: list[str] = []
    for item in configured_chat_ids:
        normalized = str(item).strip()
        if normalized and normalized not in chat_ids:
            chat_ids.append(normalized)
    chat_id = chat_ids[0] if chat_ids else ""
    if not app_id or not app_secret:
        raise RuntimeError("Feishu appId/appSecret is missing from env/config")
    if not chat_id:
        raise RuntimeError("FEISHU_HANDOFF_CHAT_ID(S) are empty")

    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "chat_id": chat_id,
        "chat_ids": chat_ids,
    }


def get_feishu_tenant_access_token(config: dict[str, Any], runtime: dict[str, Any]) -> str:
    cached_token = str(runtime.get("feishu_access_token") or "").strip()
    expires_at = float(runtime.get("feishu_access_token_expires_at") or 0)
    if cached_token and expires_at > time.time() + 30:
        return cached_token

    settings = resolve_feishu_runtime_settings(config)
    response = requests.post(
        f"{FEISHU_API_BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": settings["app_id"], "app_secret": settings["app_secret"]},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu tenant token failed: {payload.get('msg') or payload}")

    token = str(payload.get("tenant_access_token") or "").strip()
    expire_seconds = int(payload.get("expire") or 7200)
    if not token:
        raise RuntimeError("Feishu tenant token response is missing tenant_access_token")

    runtime["feishu_access_token"] = token
    runtime["feishu_access_token_expires_at"] = time.time() + max(60, expire_seconds - 120)
    return token


def feishu_request(
    config: dict[str, Any],
    runtime: dict[str, Any],
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = get_feishu_tenant_access_token(config, runtime)
    response = requests.request(
        method,
        f"{FEISHU_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=json_body,
        params=params,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = short_text(response.text or "", 2000)
        message = str(exc)
        if detail:
            message = f"{message}\n{detail}"
        raise requests.HTTPError(message, response=response) from None
    payload = response.json()
    if int(payload.get("code") or 0) != 0:
        raise RuntimeError(f"Feishu API {path} failed: {payload.get('msg') or payload}")
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def parse_feishu_message_text(item: dict[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or "text").strip()
    raw_content = str(((item.get("body") or {}).get("content")) or "")
    if not raw_content:
        return ""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content.strip()
    if msg_type == "text":
        return str(parsed.get("text") or "").strip()
    if msg_type == "post":
        post_payload = parsed
        if isinstance(parsed, dict):
            for locale_key in ("zh_cn", "en_us", "ja_jp"):
                candidate = parsed.get(locale_key)
                if isinstance(candidate, dict):
                    post_payload = candidate
                    break
        title = str(post_payload.get("title") or "").strip()
        lines: list[str] = []
        for paragraph in post_payload.get("content") or []:
            if not isinstance(paragraph, list):
                continue
            parts: list[str] = []
            for block in paragraph:
                if not isinstance(block, dict):
                    continue
                raw_text = str(block.get("text") or "")
                if raw_text.strip():
                    parts.append(raw_text)
                    continue
                tag = str(block.get("tag") or "").strip().lower()
                if tag == "at":
                    mention_name = str(block.get("user_name") or block.get("name") or "@").strip()
                    if mention_name:
                        parts.append(f"@{mention_name}" if not mention_name.startswith("@") else mention_name)
                elif tag == "a":
                    link_text = str(block.get("text") or block.get("href") or "")
                    if link_text.strip():
                        parts.append(link_text)
                elif tag == "img":
                    parts.append("[image]")
            line = "".join(parts).strip()
            if line:
                lines.append(line)
        if title:
            lines.insert(0, title)
        return "\n".join(lines).strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return str(parsed.get("text") or parsed.get("title") or "").strip()


def feishu_get_message(config: dict[str, Any], runtime: dict[str, Any], message_id: str) -> dict[str, Any]:
    payload = feishu_request(config, runtime, "GET", f"/im/v1/messages/{message_id}")
    items = payload.get("items") if isinstance(payload, dict) else None
    if isinstance(items, list) and items:
        item = items[0]
        if isinstance(item, dict):
            return item
    if isinstance(payload, dict):
        return payload
    raise RuntimeError(f"Feishu message lookup returned empty item for {message_id}")


def feishu_send_text_message(config: dict[str, Any], runtime: dict[str, Any], chat_id: str, text: str) -> str:
    payload = feishu_request(
        config,
        runtime,
        "POST",
        "/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json_body={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise RuntimeError("Feishu send message succeeded but message_id is missing")
    return message_id


def feishu_send_post_message(
    config: dict[str, Any],
    runtime: dict[str, Any],
    chat_id: str,
    title: str,
    content: list[list[dict[str, Any]]],
) -> str:
    payload = feishu_request(
        config,
        runtime,
        "POST",
        "/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json_body={
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(
                {"zh_cn": {"title": str(title or "").strip(), "content": content}},
                ensure_ascii=False,
            ),
        },
    )
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise RuntimeError("Feishu send post message succeeded but message_id is missing")
    return message_id


def feishu_reply_in_thread(
    config: dict[str, Any],
    runtime: dict[str, Any],
    root_message_id: str,
    text: str,
) -> str:
    payload = feishu_request(
        config,
        runtime,
        "POST",
        f"/im/v1/messages/{root_message_id}/reply",
        json_body={
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "msg_type": "text",
            "reply_in_thread": True,
        },
    )
    message_id = str(payload.get("message_id") or "").strip()
    if not message_id:
        raise RuntimeError("Feishu thread reply succeeded but message_id is missing")
    return message_id


def feishu_list_thread_messages(
    config: dict[str, Any],
    runtime: dict[str, Any],
    thread_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    return feishu_list_container_messages(
        config,
        runtime,
        container_id_type="thread",
        container_id=thread_id,
        limit=limit,
    )


def feishu_list_chat_messages(
    config: dict[str, Any],
    runtime: dict[str, Any],
    chat_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    return feishu_list_container_messages(
        config,
        runtime,
        container_id_type="chat",
        container_id=chat_id,
        limit=limit,
    )


def feishu_list_container_messages(
    config: dict[str, Any],
    runtime: dict[str, Any],
    *,
    container_id_type: str,
    container_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    payload = feishu_request(
        config,
        runtime,
        "GET",
        "/im/v1/messages",
        params={
            "container_id_type": container_id_type,
            "container_id": container_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": min(max(1, limit), 50),
        },
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    messages: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        sender = raw_item.get("sender") or {}
        messages.append(
            {
                "message_id": str(raw_item.get("message_id") or "").strip(),
                "thread_id": str(raw_item.get("thread_id") or "").strip(),
                "create_time": int(str(raw_item.get("create_time") or "0") or "0"),
                "sender_id": str(sender.get("id") or "").strip(),
                "sender_type": str(sender.get("sender_type") or "").strip(),
                "content": parse_feishu_message_text(raw_item),
            }
        )
    messages.sort(key=lambda item: (int(item["create_time"]), str(item["message_id"])))
    return messages


def feishu_message_marker_is_newer(
    message: dict[str, Any],
    last_seen_time: str | None,
    last_seen_message_id: str | None,
) -> bool:
    current_time = int(str(message.get("create_time") or "0") or "0")
    seen_time = int(str(last_seen_time or "0") or "0")
    if current_time != seen_time:
        return current_time > seen_time
    return str(message.get("message_id") or "") > str(last_seen_message_id or "")


def normalize_confirm_text(value: str) -> str:
    text = FEISHU_LEADING_MENTION_PATTERN.sub("", (value or "").strip())
    return re.sub(r"\s+", " ", text).lower()


def command_keyword_mentioned(candidate: str, command: str) -> bool:
    escaped = re.escape(command)
    pattern = rf"(^|[^a-z0-9._/-]){escaped}($|[^a-z0-9._-])"
    return re.search(pattern, candidate) is not None


def message_matches_natural_confirm(text: str) -> bool:
    normalized_text = normalize_confirm_text(text)
    if not normalized_text:
        return False

    candidates = [normalized_text, *[normalize_confirm_text(line) for line in text.splitlines() if line.strip()]]
    for candidate in candidates:
        compact = re.sub(r"[\s`'\"“”‘’。，、,.!！?？:：;；()（）\[\]{}<>《》_-]+", "", candidate)
        if not compact:
            continue
        if re.fullmatch(
            r"(?:请|麻烦)?(?:直接|就)?(?:开始)?(?:执行|跑|做|采用|按|选|选择|确认|同意|实施)?"
            r"(?:方案(?:1|一)|plan1)"
            r"(?:(?:开始)?执行|可以执行|确认执行|开始|吧|可以|确认|就行|即可|处理|实施|做)?",
            compact,
        ):
            return True
    return False


def message_matches_confirm_keywords(text: str, keywords: list[str]) -> bool:
    normalized_text = normalize_confirm_text(text)
    if not normalized_text:
        return False
    lines = [normalize_confirm_text(line) for line in text.splitlines() if line.strip()]
    candidates = [normalized_text, *lines]
    for keyword in keywords:
        normalized_keyword = normalize_confirm_text(keyword)
        if not normalized_keyword:
            continue
        for candidate in candidates:
            if candidate == normalized_keyword or candidate.startswith(f"{normalized_keyword} "):
                return True
            if normalized_keyword.startswith("/") and command_keyword_mentioned(candidate, normalized_keyword):
                return True
    if message_matches_natural_confirm(text):
        return True
    return False


def feishu_group_message_scope_missing(exc: Exception) -> bool:
    text = str(exc or "")
    return "im:message.group_msg" in text or "code\":230027" in text


def append_note_marker(note: str | None, marker: str) -> str:
    current = str(note or "").strip()
    if not current:
        return marker
    if marker in current:
        return current
    return f"{current} | {marker}"


def build_feishu_handoff_intro(repo_full_name: str, issue: dict[str, Any]) -> str:
    title = short_text(issue.get("title") or f"Issue #{issue['number']}", 120)
    return textwrap.dedent(
        f"""
        [Coder] 已登记 {repo_full_name}#{issue['number']}
        标题：{title}

        请在线程里先讨论方案。
        确认开始执行后，再在线程里发送 `/run`。
        """
    ).strip()
