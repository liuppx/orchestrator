from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from src.utils.helpers import ensure_dir


CODEX_RUNTIME_FILES = ("auth.json", "config.toml", "installation_id")
CODEX_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def codex_source_home_path(config: dict[str, Any]) -> Path:
    return Path(config["codex_source_home"]).expanduser().resolve(strict=False)


def codex_runtime_home_path(config: dict[str, Any]) -> Path:
    return Path(config["codex_runtime_home"]).expanduser().resolve(strict=False)


def ensure_codex_runtime_home(config: dict[str, Any]) -> Path:
    runtime_home = ensure_dir(codex_runtime_home_path(config))
    source_home = codex_source_home_path(config)
    if source_home.exists():
        for name in CODEX_RUNTIME_FILES:
            source = source_home / name
            target = runtime_home / name
            if not source.exists():
                continue
            if target.exists() and source.stat().st_mtime <= target.stat().st_mtime:
                continue
            shutil.copy2(source, target)
    return runtime_home


def build_codex_env(config: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(ensure_codex_runtime_home(config))
    return env


def parse_codex_jsonl_events(text: str) -> tuple[str | None, str | None]:
    thread_id: str | None = None
    last_text: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "").strip()
        if event_type == "thread.started":
            thread_id = str(payload.get("thread_id") or "").strip() or thread_id
            continue
        if event_type != "item.completed":
            continue
        item = payload.get("item") or {}
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != "agent_message":
            continue
        candidate = str(item.get("text") or "").strip()
        if candidate:
            last_text = candidate
    return thread_id, last_text


def parse_codex_jsonl_usage(text: str) -> dict[str, int] | None:
    last_usage: dict[str, int] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_payload = payload.get("payload") or {}
        if not isinstance(event_payload, dict):
            continue
        if str(event_payload.get("type") or "").strip() != "token_count":
            continue
        info = event_payload.get("info") or {}
        if not isinstance(info, dict):
            continue
        usage_payload = info.get("last_token_usage") or info.get("total_token_usage") or {}
        if not isinstance(usage_payload, dict):
            continue
        usage: dict[str, int] = {}
        for key in CODEX_USAGE_KEYS:
            usage[key] = int(usage_payload.get(key) or 0)
        last_usage = usage
    return last_usage


def parse_codex_jsonl_error_messages(text: str) -> list[str]:
    messages: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "").strip()
        if event_type == "error":
            message = str(payload.get("message") or "").strip()
            if message:
                messages.append(message)
            continue
        if event_type != "turn.failed":
            continue
        error_payload = payload.get("error") or {}
        if not isinstance(error_payload, dict):
            continue
        message = str(error_payload.get("message") or "").strip()
        if message:
            messages.append(message)
    return messages
