from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import src.db as db_module
from src import worker as worker_module
from src.clients import feishu_client as feishu_module
from src.clients.feishu_client import (
    append_note_marker,
    feishu_group_message_scope_missing,
    feishu_message_marker_is_newer,
    message_matches_confirm_keywords,
    resolve_feishu_runtime_settings,
)
from src.utils.helpers import ensure_dir, now_utc, short_text


ACTIVE_JOB_STATUSES = ("queued", "running")
STATE_LOCK = threading.Lock()
DISPATCH_THREAD: threading.Thread | None = None
DISCUSSION_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-discussion")
DISCUSSION_INFLIGHT: set[str] = set()
DISCUSSION_LOCK = threading.Lock()
DISCUSSION_ACK_TEXT = "收到，正在分析这个 issue 的方案。"


@dataclass
class SchedulerContext:
    config: dict[str, Any]
    runtime: dict[str, Any]
    record_issue_trigger: Callable[[dict[str, Any], str], dict[str, Any]]
    get_issue_session: Callable[[str, int], sqlite3.Row | None]
    issue_has_active_job: Callable[[str, int], bool]
    upsert_feishu_binding: Callable[..., sqlite3.Row]
    upsert_issue_session: Callable[..., sqlite3.Row]
    handle_feishu_chat_command: Callable[[str, dict[str, Any]], bool]
    reply_issue_discussion_to_feishu: Callable[..., str | None]
    confirm_feishu_binding_and_queue: Callable[[dict[str, Any], sqlite3.Row, dict[str, Any]], tuple[str, bool]]


def session_allows_feishu_followup(session_state: str | None) -> bool:
    normalized = str(session_state or "").strip().lower()
    # failed keeps the same issue/thread context, so a later `/run`
    # in the same thread should be allowed to queue a retry.
    return normalized in {"waiting_confirm", "bound", "failed"}


def feishu_discussion_key(binding: sqlite3.Row | dict[str, Any], latest_message: dict[str, Any]) -> str:
    return ":".join(
        [
            str(binding["chat_id"] or "").strip(),
            str(binding["thread_id"] or "").strip(),
            str(latest_message.get("message_id") or "").strip(),
        ]
    )


def is_fast_feishu_discussion_command(message: dict[str, Any]) -> bool:
    normalized = feishu_module.normalize_confirm_text(str(message.get("content") or ""))
    return normalized == "/model" or normalized.startswith("/model ")


def reply_feishu_discussion_now(
    context: SchedulerContext,
    repo_full_name: str,
    issue_number: int,
    *,
    binding: sqlite3.Row | dict[str, Any],
    recent_messages: list[dict[str, Any]],
) -> None:
    try:
        context.reply_issue_discussion_to_feishu(
            context.config,
            repo_full_name,
            issue_number,
            binding=binding,
            recent_messages=recent_messages,
        )
    except Exception as exc:
        error_summary = short_text(worker_module.user_friendly_error_summary(str(exc)), 700)
        print(
            f"warning: failed to proxy Feishu discussion for "
            f"{repo_full_name}#{issue_number}: {error_summary}"
        )
        root_message_id = (
            str(binding["root_message_id"] or "").strip()
            or str(binding["prompt_message_id"] or "").strip()
        )
        if root_message_id:
            try:
                feishu_module.feishu_reply_in_thread(
                    context.config,
                    context.runtime,
                    root_message_id,
                    f"讨论阶段回复失败，请稍后重试。\n\n错误摘要：{error_summary}",
                )
            except Exception as reply_exc:
                print(
                    f"warning: failed to post discussion error reply for "
                    f"{repo_full_name}#{issue_number}: {reply_exc}"
                )


def run_queued_feishu_discussion_reply(
    context: SchedulerContext,
    repo_full_name: str,
    issue_number: int,
    *,
    binding: dict[str, Any],
    recent_messages: list[dict[str, Any]],
    discussion_key: str,
) -> None:
    try:
        session_row = context.get_issue_session(repo_full_name, issue_number)
        if session_row is not None and not session_allows_feishu_followup(str(session_row["session_state"] or "")):
            return
        reply_feishu_discussion_now(
            context,
            repo_full_name,
            issue_number,
            binding=binding,
            recent_messages=recent_messages,
        )
    finally:
        with DISCUSSION_LOCK:
            DISCUSSION_INFLIGHT.discard(discussion_key)


def enqueue_feishu_discussion_reply(
    context: SchedulerContext,
    repo_full_name: str,
    issue_number: int,
    *,
    binding: sqlite3.Row,
    recent_messages: list[dict[str, Any]],
    latest_message: dict[str, Any],
) -> bool:
    discussion_key = feishu_discussion_key(binding, latest_message)
    if not discussion_key:
        return False
    with DISCUSSION_LOCK:
        if discussion_key in DISCUSSION_INFLIGHT:
            return False
        DISCUSSION_INFLIGHT.add(discussion_key)

    binding_snapshot = dict(binding)
    root_message_id = (
        str(binding_snapshot.get("root_message_id") or "").strip()
        or str(binding_snapshot.get("prompt_message_id") or "").strip()
    )
    if root_message_id:
        try:
            feishu_module.feishu_reply_in_thread(
                context.config,
                context.runtime,
                root_message_id,
                DISCUSSION_ACK_TEXT,
            )
        except Exception as exc:
            print(
                f"warning: failed to post discussion ack for "
                f"{repo_full_name}#{issue_number}: {short_text(str(exc), 800)}"
            )

    try:
        DISCUSSION_EXECUTOR.submit(
            run_queued_feishu_discussion_reply,
            context,
            repo_full_name,
            issue_number,
            binding=binding_snapshot,
            recent_messages=list(recent_messages),
            discussion_key=discussion_key,
        )
    except Exception:
        with DISCUSSION_LOCK:
            DISCUSSION_INFLIGHT.discard(discussion_key)
        raise
    return True


def default_state() -> dict[str, Any]:
    return {
        "processed_triggers": {},
        "poll_cache": {
            "repos": {},
            "issues": {},
        },
    }


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    processed = state.get("processed_triggers")
    if not isinstance(processed, dict):
        state["processed_triggers"] = {}

    poll_cache = state.get("poll_cache")
    if not isinstance(poll_cache, dict):
        state["poll_cache"] = {}
        poll_cache = state["poll_cache"]

    repos = poll_cache.get("repos")
    if not isinstance(repos, dict):
        poll_cache["repos"] = {}

    issues = poll_cache.get("issues")
    if not isinstance(issues, dict):
        poll_cache["issues"] = {}

    feishu_chats = poll_cache.get("feishu_chats")
    if not isinstance(feishu_chats, dict):
        poll_cache["feishu_chats"] = {}

    return state


def poll_cache_repo_state(state: dict[str, Any], repo_full_name: str) -> dict[str, Any]:
    normalized = normalize_state(state)
    poll_cache = normalized["poll_cache"]
    repos = poll_cache["repos"]
    repo_state = repos.get(repo_full_name)
    if not isinstance(repo_state, dict):
        repo_state = {}
        repos[repo_full_name] = repo_state
    return repo_state


def poll_cache_issue_state(state: dict[str, Any], repo_full_name: str, issue_number: int) -> dict[str, Any]:
    normalized = normalize_state(state)
    poll_cache = normalized["poll_cache"]
    issues = poll_cache["issues"]
    issue_key = poll_cache_issue_key(repo_full_name, issue_number)
    issue_state = issues.get(issue_key)
    if not isinstance(issue_state, dict):
        issue_state = {}
        issues[issue_key] = issue_state
    return issue_state


def poll_cache_feishu_chat_state(state: dict[str, Any], chat_id: str) -> dict[str, Any]:
    normalized = normalize_state(state)
    poll_cache = normalized["poll_cache"]
    chats = poll_cache["feishu_chats"]
    chat_state = chats.get(chat_id)
    if not isinstance(chat_state, dict):
        chat_state = {}
        chats[chat_id] = chat_state
    return chat_state


def poll_cache_issue_key(repo_full_name: str, issue_number: int) -> str:
    return f"{repo_full_name}#{issue_number}"


def state_set(target: dict[str, Any], key: str, value: Any) -> bool:
    current = target.get(key)
    if current == value:
        return False
    if value is None:
        if key not in target:
            return False
        target.pop(key, None)
        return True
    target[key] = value
    return True


def load_state(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["state_file"])
    ensure_dir(path.parent)
    with STATE_LOCK:
        if not path.exists():
            return default_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default_state()
    if not isinstance(data, dict):
        return default_state()
    return normalize_state(data)


def save_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    path = Path(config["state_file"])
    ensure_dir(path.parent)
    normalized = normalize_state(state)
    processed = normalized["processed_triggers"]
    if len(processed) > 2000:
        trimmed = sorted(processed.items(), key=lambda item: item[1])[-1000:]
        normalized["processed_triggers"] = dict(trimmed)
    tmp_path = path.with_suffix(".tmp")
    payload = json.dumps(normalized, ensure_ascii=False, indent=2)
    with STATE_LOCK:
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)


def remove_issue_from_state(config: dict[str, Any], repo_full_name: str, issue_number: int) -> None:
    prefix = f"{repo_full_name}#{issue_number}:"
    state = load_state(config)
    processed = state.setdefault("processed_triggers", {})
    keys = [key for key in processed if key.startswith(prefix)]
    issue_cache_key = poll_cache_issue_key(repo_full_name, issue_number)
    issue_cache = state.setdefault("poll_cache", {}).setdefault("issues", {})
    removed = False
    if not keys:
        if issue_cache.pop(issue_cache_key, None) is None:
            return
        save_state(config, state)
        return
    for key in keys:
        processed.pop(key, None)
        removed = True
    if issue_cache.pop(issue_cache_key, None) is not None:
        removed = True
    if not removed:
        return
    save_state(config, state)


def trigger_key(repo_full_name: str, issue_number: int, kind: str, value: str) -> str:
    return f"{repo_full_name}#{issue_number}:{kind}:{value}"


def repo_has_running_job(config: dict[str, Any], repo_full_name: str) -> bool:
    row = db_module.fetchone(
        config,
        "SELECT job_id FROM jobs WHERE repo_full_name = ? AND status = 'running' LIMIT 1",
        (repo_full_name,),
    )
    return row is not None


def pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def queue_payload(context: SchedulerContext, payload: dict[str, Any], reason: str) -> str:
    trigger_info = context.record_issue_trigger(payload, reason)
    action = "created" if trigger_info["session_state"] == "waiting_confirm" else "updated"
    print(
        f"{action} handoff for "
        f"{trigger_info['repo_full_name']}#{trigger_info['issue_number']} "
        f"(session={trigger_info['session_key']} state={trigger_info['session_state']})"
    )
    return str(trigger_info["session_key"])


def recover_inflight_jobs(context: SchedulerContext, *, source: str = "service startup") -> None:
    running_jobs = db_module.fetchall(
        context.config,
        "SELECT job_id, worker_pid, repo_full_name, issue_number FROM jobs WHERE status = 'running'",
    )
    for row in running_jobs:
        if pid_is_alive(row["worker_pid"]):
            continue
        repo_full_name = str(row["repo_full_name"])
        issue_number = int(row["issue_number"])
        worker_module.release_active_lock(context.config, repo_full_name, issue_number)
        worker_module.release_repo_lock(context.config, repo_full_name)
        db_module.requeue_job(
            context.config,
            str(row["job_id"]),
            f"worker process missing; re-queued on {source}",
        )


def scan_feishu_chat_commands(context: SchedulerContext) -> None:
    try:
        settings = resolve_feishu_runtime_settings(context.config)
    except Exception:
        return

    state = load_state(context.config)
    state_changed = False
    chat_ids = [
        str(item).strip()
        for item in (settings.get("chat_ids") or [])
        if str(item).strip()
    ]
    for chat_id in chat_ids:
        chat_state = poll_cache_feishu_chat_state(state, chat_id)
        last_seen_id = str(chat_state.get("last_seen_message_id") or "").strip()
        last_seen_time = str(chat_state.get("last_seen_message_time") or "").strip()

        try:
            messages = feishu_module.feishu_list_chat_messages(
                context.config,
                context.runtime,
                chat_id,
                context.config["feishu_chat_scan_limit"],
            )
        except Exception as exc:
            print(
                f"warning: failed to scan Feishu chat commands for "
                f"{chat_id}: {short_text(str(exc), 1200)}"
            )
            continue

        newest_seen_id = last_seen_id
        newest_seen_time = last_seen_time
        for message in messages:
            if not feishu_message_marker_is_newer(message, last_seen_time, last_seen_id):
                continue
            newest_seen_id = str(message.get("message_id") or newest_seen_id)
            newest_seen_time = str(message.get("create_time") or newest_seen_time)
            if str(message.get("thread_id") or "").strip():
                continue
            if str(message.get("sender_type") or "").strip().lower() != "user":
                continue
            try:
                context.handle_feishu_chat_command(chat_id, message)
            except Exception as exc:
                print(
                    f"warning: failed to handle Feishu chat command for "
                    f"{chat_id}: {short_text(str(exc), 1200)}"
                )

        state_changed |= state_set(chat_state, "last_seen_message_id", newest_seen_id or None)
        state_changed |= state_set(chat_state, "last_seen_message_time", newest_seen_time or None)
    if state_changed:
        save_state(context.config, state)


def scan_waiting_feishu_confirmations(context: SchedulerContext) -> None:
    for binding in db_module.fetch_waiting_feishu_bindings(context.config):
        repo_full_name = str(binding["repo_full_name"])
        issue_number = int(binding["issue_number"])
        session_row = context.get_issue_session(repo_full_name, issue_number)
        if session_row is None:
            continue
        if not session_allows_feishu_followup(str(session_row["session_state"] or "")):
            continue
        if context.issue_has_active_job(repo_full_name, issue_number):
            continue

        thread_id = str(binding["thread_id"] or "").strip()
        if not thread_id:
            continue
        try:
            messages = feishu_module.feishu_list_thread_messages(
                context.config,
                context.runtime,
                thread_id,
                context.config["feishu_thread_scan_limit"],
            )
        except Exception as exc:
            error_summary = short_text(str(exc), 1500)
            print(
                f"warning: failed to scan Feishu thread for "
                f"{repo_full_name}#{issue_number}: {error_summary}"
            )
            if feishu_group_message_scope_missing(exc):
                warning_marker = "warning:missing-im-message-group-msg-scope"
                note = str(binding["note"] or "")
                warning_text = (
                    "当前飞书应用缺少 `im:message.group_msg` 权限，"
                    "暂时无法读取群线程消息，因此不会响应讨论消息或 `/run`。\n\n"
                    "请在飞书开放平台为该应用开通这个权限后，再回到这个线程重试。"
                )
                root_message_id = (
                    str(binding["root_message_id"] or "").strip()
                    or str(binding["prompt_message_id"] or "").strip()
                )
                if warning_marker not in note and root_message_id:
                    try:
                        feishu_module.feishu_reply_in_thread(
                            context.config,
                            context.runtime,
                            root_message_id,
                            warning_text,
                        )
                    except Exception as reply_exc:
                        print(
                            f"warning: failed to post Feishu permission warning for "
                            f"{repo_full_name}#{issue_number}: {short_text(str(reply_exc), 800)}"
                        )
                if warning_marker not in note:
                    context.upsert_feishu_binding(
                        context.config,
                        chat_id=str(binding["chat_id"] or "").strip(),
                        thread_id=thread_id,
                        repo_full_name=repo_full_name,
                        issue_number=issue_number,
                        session_key=str(binding["session_key"] or "").strip(),
                        binding_state=str(binding["binding_state"] or "waiting_confirm"),
                        note=append_note_marker(note, warning_marker),
                        root_message_id=str(binding["root_message_id"] or "").strip() or None,
                        prompt_message_id=str(binding["prompt_message_id"] or "").strip() or None,
                        last_seen_message_id=str(binding["last_seen_message_id"] or "").strip() or None,
                        last_seen_message_time=str(binding["last_seen_message_time"] or "").strip() or None,
                        confirm_message_id=str(binding["confirm_message_id"] or "").strip() or None,
                        confirm_message_time=str(binding["confirm_message_time"] or "").strip() or None,
                    )
                    context.upsert_issue_session(
                        context.config,
                        repo_full_name,
                        issue_number,
                        summary=warning_text,
                        last_result_status="waiting_confirm",
                    )
            continue

        newest_seen_id = str(binding["last_seen_message_id"] or "").strip()
        newest_seen_time = str(binding["last_seen_message_time"] or "").strip()
        confirm_message: dict[str, Any] | None = None
        discussion_messages: list[dict[str, Any]] = []

        for message in messages:
            if not feishu_message_marker_is_newer(
                message,
                str(binding["last_seen_message_time"] or ""),
                str(binding["last_seen_message_id"] or ""),
            ):
                continue
            newest_seen_id = str(message["message_id"])
            newest_seen_time = str(message["create_time"])
            if str(message.get("sender_type") or "").strip().lower() != "user":
                continue
            if message_matches_confirm_keywords(
                str(message.get("content") or ""),
                context.config["feishu_confirm_keywords"],
            ):
                confirm_message = message
                break
            discussion_messages.append(message)

        if discussion_messages and confirm_message is None:
            latest_message = discussion_messages[-1]
            visible_messages = []
            for item in messages:
                visible_messages.append(item)
                if str(item.get("message_id") or "") == str(latest_message.get("message_id") or ""):
                    break
            if is_fast_feishu_discussion_command(latest_message):
                reply_feishu_discussion_now(
                    context,
                    repo_full_name,
                    issue_number,
                    binding=binding,
                    recent_messages=visible_messages,
                )
            else:
                try:
                    enqueue_feishu_discussion_reply(
                        context,
                        repo_full_name,
                        issue_number,
                        binding=binding,
                        recent_messages=visible_messages,
                        latest_message=latest_message,
                    )
                except Exception as exc:
                    error_summary = short_text(worker_module.user_friendly_error_summary(str(exc)), 700)
                    print(
                        f"warning: failed to enqueue Feishu discussion for "
                        f"{repo_full_name}#{issue_number}: {error_summary}"
                    )

        if confirm_message is None:
            if newest_seen_id != str(binding["last_seen_message_id"] or "").strip() or newest_seen_time != str(
                binding["last_seen_message_time"] or ""
            ).strip():
                context.upsert_feishu_binding(
                    context.config,
                    chat_id=str(binding["chat_id"]),
                    thread_id=thread_id,
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    session_key=str(binding["session_key"]),
                    note=str(binding["note"] or "") or None,
                    binding_state=str(binding["binding_state"] or "waiting_confirm"),
                    root_message_id=str(binding["root_message_id"] or "") or None,
                    prompt_message_id=str(binding["prompt_message_id"] or "") or None,
                    last_seen_message_id=newest_seen_id or None,
                    last_seen_message_time=newest_seen_time or None,
                    confirm_message_id=str(binding["confirm_message_id"] or "") or None,
                    confirm_message_time=str(binding["confirm_message_time"] or "") or None,
                )
            continue

        job_id, created = context.confirm_feishu_binding_and_queue(context.config, binding, confirm_message)
        context.runtime["last_queued_job_id"] = job_id
        action = "queued" if created else "reused"
        print(
            f"{action} job {job_id} for "
            f"{repo_full_name}#{issue_number} "
            f"(session={session_row['session_key']} state=queued)"
        )


def dispatch_queued_jobs(context: SchedulerContext) -> None:
    queued_jobs = db_module.fetchall(
        context.config,
        "SELECT job_id, repo_full_name, job_dir FROM jobs WHERE status = 'queued' ORDER BY created_at ASC",
    )
    repo_started: set[str] = set()
    for row in queued_jobs:
        repo_full_name = str(row["repo_full_name"])
        if repo_full_name in repo_started:
            continue
        if repo_has_running_job(context.config, repo_full_name):
            continue
        if worker_module.repo_lock_path(context.config, repo_full_name).exists():
            continue
        job_id = str(row["job_id"])
        job_dir = Path(str(row["job_dir"]))
        pid = worker_module.spawn_worker(context.config, job_id, job_dir)
        db_module.mark_job_running(context.config, job_id, pid)
        context.runtime["last_dispatched_job_id"] = job_id
        repo_started.add(repo_full_name)


def dispatch_loop(context: SchedulerContext) -> None:
    interval = max(2, context.config["dispatch_interval_seconds"])
    while True:
        try:
            recover_inflight_jobs(context, source="dispatch loop")
            scan_feishu_chat_commands(context)
            scan_waiting_feishu_confirmations(context)
            dispatch_queued_jobs(context)
        except Exception as exc:
            print(f"dispatch error: {exc}")
        time.sleep(interval)


def start_dispatch_thread(context: SchedulerContext) -> None:
    global DISPATCH_THREAD
    if DISPATCH_THREAD and DISPATCH_THREAD.is_alive():
        return
    DISPATCH_THREAD = threading.Thread(
        target=dispatch_loop,
        args=(context,),
        name="job-dispatcher",
        daemon=True,
    )
    DISPATCH_THREAD.start()


def queue_stats(config: dict[str, Any]) -> dict[str, int]:
    rows = db_module.fetchall(config, "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status")
    stats = {str(row["status"]): int(row["total"]) for row in rows}
    return {
        "queued": stats.get("queued", 0),
        "running": stats.get("running", 0),
        "succeeded": stats.get("succeeded", 0),
        "failed": stats.get("failed", 0),
        "no_change": stats.get("no_change", 0),
        "needs_human": stats.get("needs_human", 0),
        "cancelled": stats.get("cancelled", 0),
    }


__all__ = [
    "SchedulerContext",
    "default_state",
    "dispatch_loop",
    "dispatch_queued_jobs",
    "load_state",
    "queue_stats",
    "recover_inflight_jobs",
    "remove_issue_from_state",
    "scan_waiting_feishu_confirmations",
    "save_state",
    "session_allows_feishu_followup",
    "start_dispatch_thread",
]
