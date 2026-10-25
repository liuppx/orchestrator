from __future__ import annotations

import json
import re
import sqlite3
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import src.db as db_module
from src import worker as worker_module
from src.clients import feishu_client as feishu_module
from src.config import DEFAULT_SUPPORTED_CLAUDE_MODELS, DEFAULT_SUPPORTED_CODEX_MODELS
from src.clients.feishu_client import (
    build_feishu_handoff_intro,
    build_feishu_thread_session_key,
    is_feishu_route_session_key,
    normalize_confirm_text,
    resolve_feishu_runtime_settings,
)
from src.clients.github_client import (
    comment_issue,
    get_installation_token,
    github_request,
    list_open_issues,
    set_issue_status_label,
)
from src.utils.helpers import (
    now_utc,
    service_actor_name,
    short_text,
    slugify,
)


ACTIVE_JOB_STATUSES = {"queued", "running"}
ISSUE_COMMAND_PATTERN = re.compile(r"(?mi)^\s*/issue\s+([a-z0-9][a-z0-9._-]*)(?:\s*#?\s*(\d+))?(.*?)\s*$")
HELP_COMMAND_PATTERN = re.compile(r"(?mi)^\s*/help\s*$")
MODEL_COMMAND_PATTERN = re.compile(r"(?mi)^\s*/model\s+([a-z0-9][a-z0-9._-]*)\s*$")
MODEL_RESET_COMMAND_PATTERN = re.compile(r"(?mi)^\s*/model\s+(?:default|reset)\s*$")
DISCUSSION_REPLY_SESSION_STATES = {"waiting_confirm", "bound", "failed"}


def row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def session_allows_discussion_reply(session_state: str | None) -> bool:
    return str(session_state or "").strip().lower() in DISCUSSION_REPLY_SESSION_STATES


def build_repo_alias_map(config: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for repo_full_name in config.get("allowed_repos", []):
        repo_name = str(repo_full_name).split("/", 1)[-1].strip().lower()
        if repo_name and repo_name not in aliases:
            aliases[repo_name] = str(repo_full_name)
    for alias, repo_full_name in (config.get("repo_aliases") or {}).items():
        normalized_alias = str(alias or "").strip().lower()
        normalized_repo = str(repo_full_name or "").strip()
        if normalized_alias and normalized_repo:
            aliases[normalized_alias] = normalized_repo
    return aliases


def configured_models(
    config: dict[str, Any] | None,
    key: str,
    defaults: tuple[str, ...],
) -> tuple[str, ...]:
    values = (config or {}).get(key)
    if not isinstance(values, (list, tuple)):
        return defaults
    normalized = tuple(str(item).strip() for item in values if str(item).strip())
    return normalized or defaults


def supported_codex_models(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    return configured_models(config, "supported_codex_models", DEFAULT_SUPPORTED_CODEX_MODELS)


def supported_claude_models(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    return configured_models(config, "supported_claude_models", DEFAULT_SUPPORTED_CLAUDE_MODELS)


def supported_models_for_backend(
    config: dict[str, Any] | None,
    backend: str,
) -> tuple[str, ...]:
    if str(backend).strip().lower() == "claude":
        return supported_claude_models(config)
    return supported_codex_models(config)


def supported_models_union(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in (*supported_codex_models(config), *supported_claude_models(config)):
        if model in seen:
            continue
        seen.add(model)
        ordered.append(model)
    return tuple(ordered)


def backend_for_model(config: dict[str, Any], model: str | None) -> str | None:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None
    if normalized in {item.lower() for item in supported_codex_models(config)}:
        return "codex"
    if normalized in {item.lower() for item in supported_claude_models(config)}:
        return "claude"
    return None


def default_backend_name(config: dict[str, Any]) -> str:
    return str(config.get("execution_backend") or "codex").strip().lower() or "codex"


def default_model_for_backend(config: dict[str, Any], backend: str) -> str | None:
    backend_name = str(backend).strip().lower()
    default_model_key = "claude_model" if backend_name == "claude" else "codex_model"
    default_model = str(config.get(default_model_key) or "").strip()
    return default_model or None


def resolve_model_backend(
    config: dict[str, Any],
    *,
    backend: str | None = None,
    selected_model: str | None = None,
) -> tuple[str, str | None]:
    model_backend = backend_for_model(config, selected_model)
    effective_backend = model_backend or str(backend or default_backend_name(config)).strip().lower() or "codex"
    effective_model = str(selected_model or "").strip() or default_model_for_backend(config, effective_backend)
    return effective_backend, effective_model


def example_model_for_backend(config: dict[str, Any], backend: str) -> str:
    backend_name = str(backend).strip().lower()
    default_model_key = "claude_model" if backend_name == "claude" else "codex_model"
    default_model = str(config.get(default_model_key) or "").strip()
    candidates = supported_models_for_backend(config, backend_name)
    if default_model and default_model in candidates:
        return default_model
    return candidates[0] if candidates else default_model or "(default)"


def parse_feishu_issue_command(text: str) -> tuple[str, int | None, dict[str, str]] | None:
    match = ISSUE_COMMAND_PATTERN.match(normalize_confirm_text(text))
    if not match:
        return None
    alias = str(match.group(1) or "").strip().lower()
    issue_number_raw = str(match.group(2) or "").strip()
    params_raw = str(match.group(3) or "").strip()
    if params_raw and not issue_number_raw:
        return None
    params = parse_trailing_params(params_raw)
    return alias, (int(issue_number_raw) if issue_number_raw else None), params


def parse_trailing_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    extra_positionals: list[str] = []
    for token in (raw or "").split():
        if "=" in token:
            params["_legacy"] = " ".join(
                part for part in [params.get("_legacy", ""), token.strip()] if part
            )
            continue

        value = token.strip().lower()
        if not value:
            continue
        if "executor" not in params and value in {"codex", "claude"}:
            params["executor"] = value
            continue
        if "model" not in params:
            params["model"] = value
            continue
        extra_positionals.append(value)

    if extra_positionals:
        params["_extra"] = " ".join(extra_positionals)
    return params


def validate_issue_params(
    params: dict[str, str],
    default_backend: str,
    config: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Returns (backend, model, error_message)."""
    if params.get("_legacy"):
        return None, None, (
            "不再支持 `executor=... model=...` 参数格式。\n"
            "请直接使用模型名，例如：`/issue deployer #108 gpt-5.4` 或 "
            "`/issue deployer #108 claude-opus-4-6`"
        )
    if params.get("_extra"):
        return None, None, (
            f"无法识别参数 `{params['_extra']}`。\n"
            "示例：`/issue deployer #108 gpt-5.4` 或 "
            "`/issue deployer #108 claude-opus-4-6`"
        )
    if params.get("executor"):
        return None, None, (
            "不需要再单独指定执行器了。\n"
            "请直接使用模型名，例如：`/issue deployer #108 gpt-5.5`、"
            "`/issue deployer #108 claude-opus-4-6`，或者发送 `/model <name>`。"
        )
    model = params.get("model")
    if model:
        backend = backend_for_model(config or {}, model)
        if backend is None:
            allowed_models = ", ".join(supported_models_union(config))
            return None, None, (
                f"不支持模型 `{model}`。\n"
                f"当前可用模型：{allowed_models}"
            )
        return backend, model, None
    return None, None, None


def resolve_executor_model_display(
    config: dict[str, Any],
    backend: str | None,
    selected_model: str | None,
    *,
    actual_backend: str | None = None,
    actual_model: str | None = None,
) -> tuple[str, str]:
    planned_backend, planned_model = resolve_model_backend(
        config,
        backend=backend,
        selected_model=selected_model,
    )
    effective_backend = str(actual_backend or planned_backend).strip().lower() or planned_backend
    effective_model = str(actual_model or planned_model or "").strip()
    backend_display = effective_backend.capitalize()
    model_display = effective_model or "(default)"
    if backend_display:
        model_display = f"{model_display} ({backend_display})"
    planned_selected_model = str(selected_model or "").strip()
    if planned_selected_model and effective_model and planned_selected_model != effective_model:
        model_display = f"{model_display}，请求模型：{planned_selected_model}"
    return backend_display, model_display


def format_issue_list_timestamp(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    normalized = raw.replace("T", " ").rstrip("Z")
    return normalized[:19]


def is_feishu_help_command(text: str) -> bool:
    return HELP_COMMAND_PATTERN.match(normalize_confirm_text(text)) is not None


def parse_feishu_model_command(text: str) -> str | None:
    normalized = normalize_confirm_text(text)
    if MODEL_RESET_COMMAND_PATTERN.match(normalized):
        return ""
    match = MODEL_COMMAND_PATTERN.match(normalized)
    if not match:
        return None
    return str(match.group(1) or "").strip().lower()


@dataclass
class IssueService:
    config: dict[str, Any]
    runtime: dict[str, Any]

    def feishu_get_message(self, message_id: str) -> dict[str, Any]:
        return feishu_module.feishu_get_message(self.config, self.runtime, message_id)

    def feishu_send_text_message(self, chat_id: str, text: str) -> str:
        return feishu_module.feishu_send_text_message(self.config, self.runtime, chat_id, text)

    def feishu_send_post_message(
        self,
        chat_id: str,
        title: str,
        content: list[list[dict[str, Any]]],
    ) -> str:
        return feishu_module.feishu_send_post_message(self.config, self.runtime, chat_id, title, content)

    def feishu_reply_in_thread(self, root_message_id: str, text: str) -> str:
        return feishu_module.feishu_reply_in_thread(self.config, self.runtime, root_message_id, text)

    def feishu_list_thread_messages(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        return feishu_module.feishu_list_thread_messages(self.config, self.runtime, thread_id, limit)

    def fetchone(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return db_module.fetchone(self.config, query, params)

    def fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return db_module.fetchall(self.config, query, params)

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        db_module.execute(self.config, query, params)

    def record_delivery_once(self, delivery_id: str, event_name: str) -> bool:
        return db_module.record_delivery_once(self.config, delivery_id, event_name)

    def upsert_issue_record(
        self,
        repo_full_name: str,
        issue_number: int,
        issue_title: str,
        issue_state: str,
        *,
        active_job_id: str | None = None,
        last_reason: str | None = None,
    ) -> None:
        db_module.upsert_issue_record(
            self.config,
            repo_full_name,
            issue_number,
            issue_title,
            issue_state,
            active_job_id=active_job_id,
            last_reason=last_reason,
        )

    def clear_issue_active_job(self, repo_full_name: str, issue_number: int, job_id: str) -> None:
        db_module.clear_issue_active_job(self.config, repo_full_name, issue_number, job_id)

    def get_existing_active_job(self, repo_full_name: str, issue_number: int) -> sqlite3.Row | None:
        return db_module.get_existing_active_job(
            self.config,
            repo_full_name,
            issue_number,
            tuple(ACTIVE_JOB_STATUSES),
        )

    def issue_has_active_job(self, repo_full_name: str, issue_number: int) -> bool:
        return self.get_existing_active_job(repo_full_name, issue_number) is not None

    def build_issue_session_key(self, repo_full_name: str, issue_number: int) -> str:
        prefix = slugify(self.config.get("issue_session_prefix", "gh"), limit=16)
        repo_slug = slugify(repo_full_name.replace("/", "-"), limit=48)
        if prefix:
            return f"{prefix}-{repo_slug}-issue-{issue_number}"
        return f"{repo_slug}-issue-{issue_number}"

    def normalize_issue_session_key(
        self,
        repo_full_name: str,
        issue_number: int,
        session_key: str | None,
    ) -> str:
        expected = self.build_issue_session_key(repo_full_name, issue_number)
        candidate = str(session_key or "").strip()
        if not candidate or is_feishu_route_session_key(candidate):
            return expected
        return candidate

    def build_issue_branch_name(self, issue_number: int, issue_title: str) -> str:
        prefix = slugify(self.config.get("issue_branch_prefix", "coder"), limit=16) or "coder"
        title_slug = slugify(issue_title or "task", limit=32)
        return f"{prefix}/issue-{issue_number}-{title_slug}"

    def build_issue_agent_id(self, repo_full_name: str, issue_number: int) -> str:
        return worker_module.build_issue_agent_id(self.config, repo_full_name, issue_number)

    def repo_alias_map(self) -> dict[str, str]:
        return build_repo_alias_map(self.config)

    def resolve_repo_alias(self, alias: str) -> str | None:
        return self.repo_alias_map().get(str(alias or "").strip().lower())

    def issue_progress_label(self, repo_full_name: str, issue_number: int) -> str:
        active_job = self.get_existing_active_job(repo_full_name, issue_number)
        if active_job is not None:
            return str(active_job["status"])
        session_row = self.get_issue_session(repo_full_name, issue_number)
        if session_row is None:
            return "open"
        session_state = str(session_row["session_state"] or "").strip().lower()
        if not session_state or session_state in {"done", "closed"}:
            return "open"
        return session_state

    def list_pending_repo_issues(self, repo_full_name: str, limit: int = 10) -> list[dict[str, Any]]:
        owner, repo = repo_full_name.split("/", 1)
        token = get_installation_token(self.config)
        issues, _, _ = list_open_issues(self.config, token, owner, repo)
        result: list[dict[str, Any]] = []
        for issue in issues:
            if issue.get("pull_request"):
                continue
            issue_number = int(issue["number"])
            result.append(
                {
                    "number": issue_number,
                    "title": str(issue.get("title") or f"Issue #{issue_number}"),
                    "created_at": str(issue.get("created_at") or ""),
                    "html_url": str(issue.get("html_url") or ""),
                    "updated_at": str(issue.get("updated_at") or ""),
                    "progress": self.issue_progress_label(repo_full_name, issue_number),
                }
            )
            if len(result) >= max(1, limit):
                break
        return result

    def build_pending_issue_list_message(
        self, repo_full_name: str, issues: list[dict[str, Any]]
    ) -> str:
        if not issues:
            return f"{repo_full_name} 当前没有 open issue。"
        lines = [f"{repo_full_name} 当前 open issues：", "Issue | 创建时间 | 标题"]
        for issue in issues:
            lines.append(
                f"#{issue['number']} | {format_issue_list_timestamp(str(issue.get('created_at') or ''))} | "
                f"{str(issue.get('title') or '')}"
            )
        return "\n".join(lines).strip()

    def build_pending_issue_list_post_message(
        self,
        repo_full_name: str,
        issues: list[dict[str, Any]],
    ) -> tuple[str, list[list[dict[str, Any]]]]:
        title = f"{repo_full_name} 当前 open issues："
        content: list[list[dict[str, Any]]] = [[{"tag": "text", "text": "Issue | 创建时间 | 标题"}]]
        for issue in issues:
            content.append(
                [
                    {
                        "tag": "a",
                        "text": f"#{issue['number']}",
                        "href": str(issue.get("html_url") or "").strip(),
                    },
                    {
                        "tag": "text",
                        "text": (
                            f" | {format_issue_list_timestamp(str(issue.get('created_at') or ''))}"
                            f" | {str(issue.get('title') or '')}"
                        ),
                    },
                ]
            )
        return title, content

    def build_feishu_help_message(self) -> str:
        codex_models = "、".join(f"`{model}`" for model in supported_codex_models(self.config))
        claude_models = "、".join(f"`{model}`" for model in supported_claude_models(self.config))
        return textwrap.dedent(
            f"""
            [Coder] 使用方式：

            1. 发送 `/issue <repo>`
            查看该仓库当前待处理的 issue 列表。

            2. 发送 `/issue <repo> #<issue_number>`
            或 `/issue <repo> <issue_number>`
            进入这个 issue 的线程会话。

            进入线程后：
            - 直接讨论方案、边界和风险
            - 发送 `/model <name>` 切换当前 issue 模型
            - 不需要再手动指定执行器；系统会按模型自动映射到 Codex 或 Claude
            - 可用 Codex 模型：{codex_models}
            - 可用 Claude 模型：{claude_models}
            - 切换示例：`/model gpt-5.4`、`/model claude-opus-4-6`、`/model default`
            - 选 issue 时也可直接带模型：`/issue deployer #108 gpt-5.5`
            - 发送 `/run`、`执行方案1` 或 `方案1` 开始执行
            """
        ).strip()

    def primary_feishu_chat_id(self) -> str:
        settings = resolve_feishu_runtime_settings(self.config)
        return str(settings["chat_id"])

    def handle_feishu_chat_command(self, chat_id: str, message: dict[str, Any]) -> bool:
        if is_feishu_help_command(str(message.get("content") or "")):
            self.feishu_send_text_message(chat_id, self.build_feishu_help_message())
            return True

        parsed = parse_feishu_issue_command(str(message.get("content") or ""))
        if parsed is None:
            return False

        repo_alias, issue_number, params = parsed
        repo_full_name = self.resolve_repo_alias(repo_alias)
        if not repo_full_name:
            known = ", ".join(sorted(self.repo_alias_map().keys())[:12]) or "(none)"
            self.feishu_send_text_message(
                chat_id,
                f"[Coder] 未找到仓库别名 `{repo_alias}`。\n\n当前可用别名：{known}",
            )
            return True

        if issue_number is None:
            try:
                issues = self.list_pending_repo_issues(repo_full_name, limit=10)
            except Exception as exc:
                self.feishu_send_text_message(
                    chat_id,
                    f"[Coder] 读取 {repo_full_name} 的 issue 列表失败：{short_text(str(exc), 1200)}",
                )
                return True
            if not issues:
                self.feishu_send_text_message(chat_id, self.build_pending_issue_list_message(repo_full_name, issues))
                return True
            title, content = self.build_pending_issue_list_post_message(repo_full_name, issues)
            self.feishu_send_post_message(chat_id, title, content)
            return True

        backend, model, param_error = validate_issue_params(
            params,
            self.config["execution_backend"],
            self.config,
        )
        if param_error:
            self.feishu_send_text_message(
                chat_id,
                f"[Coder] {param_error}",
            )
            return True

        try:
            payload = self.build_issue_payload_from_github(repo_full_name, issue_number)
        except Exception as exc:
            self.feishu_send_text_message(
                chat_id,
                f"[Coder] 读取 {repo_full_name}#{issue_number} 失败：{short_text(str(exc), 1200)}",
            )
            return True

        issue = payload.get("issue") or {}
        if issue.get("pull_request"):
            self.feishu_send_text_message(
                chat_id,
                f"[Coder] {repo_full_name}#{issue_number} 是 Pull Request，不是 issue。",
            )
            return True
        if str(issue.get("state") or "").strip().lower() != "open":
            self.feishu_send_text_message(
                chat_id,
                f"[Coder] {repo_full_name}#{issue_number} 当前不是 open 状态，不能发起新会话。",
            )
            return True

        self.record_issue_trigger(
            payload,
            f"feishu.issue_select:{chat_id}:{repo_alias}:{issue_number}",
            chat_id=chat_id,
            backend=backend,
            model=model,
        )
        return True

    def get_issue_session(self, repo_full_name: str, issue_number: int) -> sqlite3.Row | None:
        return db_module.get_issue_session(self.config, repo_full_name, issue_number)

    def upsert_issue_session(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        backend: str | None = None,
        selected_model: str | None = None,
        clear_selected_model: bool = False,
        clear_agent_session_id: bool = False,
        session_key: str | None = None,
        session_state: str | None = None,
        last_trigger_reason: str | None = None,
        last_triggered_at: str | None = None,
        handoff_prompt: str | None = None,
        agent_session_id: str | None = None,
        branch_name: str | None = None,
        pr_url: str | None = None,
        summary: str | None = None,
        last_result_status: str | None = None,
    ) -> sqlite3.Row:
        existing = self.get_issue_session(repo_full_name, issue_number)
        created_at = str(existing["created_at"]) if existing else now_utc()
        existing_backend = str(existing["backend"]) if existing and existing["backend"] else self.config["execution_backend"]
        final_backend = str(backend or existing_backend or self.config["execution_backend"]).strip().lower() or self.config["execution_backend"]
        final_session_key = self.normalize_issue_session_key(
            repo_full_name,
            issue_number,
            session_key
            if session_key is not None
            else (str(existing["session_key"]) if existing and existing["session_key"] else None),
        )
        record = {
            "backend": final_backend,
            "selected_model": (
                None
                if clear_selected_model
                else (
                    selected_model
                    if selected_model is not None
                    else (str(existing["selected_model"]) if existing and existing["selected_model"] else None)
                )
            ),
            "session_key": final_session_key,
            "session_state": session_state
            or (str(existing["session_state"]) if existing and existing["session_state"] else "triggered"),
            "last_trigger_reason": (
                last_trigger_reason
                if last_trigger_reason is not None
                else (str(existing["last_trigger_reason"]) if existing and existing["last_trigger_reason"] else None)
            ),
            "last_triggered_at": (
                last_triggered_at
                if last_triggered_at is not None
                else (str(existing["last_triggered_at"]) if existing and existing["last_triggered_at"] else None)
            ),
            "handoff_prompt": (
                short_text(handoff_prompt, 20000)
                if handoff_prompt is not None
                else (str(existing["handoff_prompt"]) if existing and existing["handoff_prompt"] else None)
            ),
            "agent_session_id": (
                None
                if clear_agent_session_id
                else (
                    agent_session_id
                    if agent_session_id is not None
                    else (str(existing["agent_session_id"]) if existing and existing["agent_session_id"] else None)
                )
            ),
            "branch_name": branch_name
            or (
                str(existing["branch_name"])
                if existing
                else self.build_issue_branch_name(issue_number, f"Issue {issue_number}")
            ),
            "pr_url": pr_url if pr_url is not None else (str(existing["pr_url"]) if existing and existing["pr_url"] else None),
            "summary": (
                short_text(summary, 12000)
                if summary is not None
                else (str(existing["summary"]) if existing and existing["summary"] else None)
            ),
            "last_result_status": (
                last_result_status
                if last_result_status is not None
                else (str(existing["last_result_status"]) if existing and existing["last_result_status"] else None)
            ),
        }
        if backend is not None and final_backend != existing_backend and not clear_agent_session_id:
            record["agent_session_id"] = None
        updated_at = now_utc()
        db_module.upsert_issue_session(
            self.config,
            repo_full_name,
            issue_number,
            backend=str(record["backend"]),
            selected_model=record["selected_model"],
            session_key=str(record["session_key"]),
            session_state=str(record["session_state"]),
            last_trigger_reason=record["last_trigger_reason"],
            last_triggered_at=record["last_triggered_at"],
            handoff_prompt=record["handoff_prompt"],
            agent_session_id=record["agent_session_id"],
            branch_name=str(record["branch_name"]),
            pr_url=record["pr_url"],
            summary=record["summary"],
            last_result_status=record["last_result_status"],
            created_at=created_at,
            updated_at=updated_at,
        )
        session = self.get_issue_session(repo_full_name, issue_number)
        if session is None:
            raise RuntimeError(f"failed to load issue session for {repo_full_name}#{issue_number}")
        return session

    def ensure_issue_session(
        self,
        repo_full_name: str,
        issue_number: int,
        issue_title: str,
    ) -> sqlite3.Row:
        existing = self.get_issue_session(repo_full_name, issue_number)
        branch_name = str(existing["branch_name"]) if existing and existing["branch_name"] else self.build_issue_branch_name(
            issue_number,
            issue_title,
        )
        session_key = self.normalize_issue_session_key(
            repo_full_name,
            issue_number,
            str(existing["session_key"]) if existing and existing["session_key"] else None,
        )
        return self.upsert_issue_session(
            repo_full_name,
            issue_number,
            backend=(
                str(existing["backend"])
                if existing and existing["backend"]
                else self.config["execution_backend"]
            ),
            selected_model=(
                str(existing["selected_model"])
                if existing and existing["selected_model"]
                else None
            ),
            session_key=session_key,
            session_state=(
                str(existing["session_state"])
                if existing and existing["session_state"]
                else "triggered"
            ),
            branch_name=branch_name,
        )

    def get_feishu_binding(self, chat_id: str, thread_id: str) -> sqlite3.Row | None:
        return db_module.get_feishu_binding(self.config, chat_id, thread_id)

    def list_issue_bindings(self, repo_full_name: str, issue_number: int) -> list[sqlite3.Row]:
        return db_module.list_issue_bindings(self.config, repo_full_name, issue_number)

    def list_issue_jobs(self, repo_full_name: str, issue_number: int) -> list[sqlite3.Row]:
        return db_module.list_jobs_for_issue(self.config, repo_full_name, issue_number)

    def upsert_feishu_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
        repo_full_name: str,
        issue_number: int,
        session_key: str | None = None,
        note: str | None = None,
        binding_state: str = "bound",
        root_message_id: str | None = None,
        prompt_message_id: str | None = None,
        last_seen_message_id: str | None = None,
        last_seen_message_time: str | None = None,
        confirm_message_id: str | None = None,
        confirm_message_time: str | None = None,
    ) -> sqlite3.Row:
        if self.get_issue_session(repo_full_name, issue_number) is None:
            self.ensure_issue_session(repo_full_name, issue_number, f"Issue {issue_number}")
        existing = self.get_feishu_binding(chat_id, thread_id)
        now = now_utc()
        final_session_key = (
            session_key
            or (str(existing["session_key"]) if existing and existing["session_key"] else "")
            or build_feishu_thread_session_key(self.config, repo_full_name, issue_number, chat_id, thread_id)
        )
        payload = {
            "note": note if note is not None else (str(existing["note"]) if existing and existing["note"] else None),
            "root_message_id": (
                root_message_id
                if root_message_id is not None
                else (str(existing["root_message_id"]) if existing and existing["root_message_id"] else None)
            ),
            "prompt_message_id": (
                prompt_message_id
                if prompt_message_id is not None
                else (str(existing["prompt_message_id"]) if existing and existing["prompt_message_id"] else None)
            ),
            "last_seen_message_id": (
                last_seen_message_id
                if last_seen_message_id is not None
                else (str(existing["last_seen_message_id"]) if existing and existing["last_seen_message_id"] else None)
            ),
            "last_seen_message_time": (
                last_seen_message_time
                if last_seen_message_time is not None
                else (str(existing["last_seen_message_time"]) if existing and existing["last_seen_message_time"] else None)
            ),
            "confirm_message_id": (
                confirm_message_id
                if confirm_message_id is not None
                else (str(existing["confirm_message_id"]) if existing and existing["confirm_message_id"] else None)
            ),
            "confirm_message_time": (
                confirm_message_time
                if confirm_message_time is not None
                else (str(existing["confirm_message_time"]) if existing and existing["confirm_message_time"] else None)
            ),
        }
        db_module.upsert_feishu_binding(
            self.config,
            chat_id=chat_id,
            thread_id=thread_id,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            session_key=final_session_key,
            binding_state=binding_state,
            note=payload["note"],
            root_message_id=payload["root_message_id"],
            prompt_message_id=payload["prompt_message_id"],
            last_seen_message_id=payload["last_seen_message_id"],
            last_seen_message_time=payload["last_seen_message_time"],
            confirm_message_id=payload["confirm_message_id"],
            confirm_message_time=payload["confirm_message_time"],
            created_at=now,
            updated_at=now,
        )
        binding = self.get_feishu_binding(chat_id, thread_id)
        if binding is None:
            raise RuntimeError(f"failed to load Feishu binding for {chat_id}:{thread_id}")
        return binding

    def delete_feishu_binding(self, chat_id: str, thread_id: str) -> bool:
        return db_module.delete_feishu_binding(self.config, chat_id, thread_id)

    def preferred_issue_binding(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        chat_id: str | None = None,
    ) -> sqlite3.Row | None:
        bindings = self.list_issue_bindings(repo_full_name, issue_number)
        if not bindings:
            return None
        if chat_id:
            for binding in bindings:
                if str(binding["chat_id"]) == chat_id:
                    return binding
        return bindings[0]

    def build_handoff_prompt(
        self,
        repo_full_name: str,
        issue: dict[str, Any],
        session_key: str | None = None,
    ) -> str:
        issue_body = short_text(issue.get("body") or "(no issue body)", 6000)
        issue_title = issue.get("title") or f"Issue #{issue['number']}"
        lines = [
            f"你正在继续处理 GitHub Issue #{issue['number']}。",
            f"仓库：{repo_full_name}",
            f"标题：{issue_title}",
        ]
        if session_key:
            lines.append(f"会话标识：{session_key}")
        lines.extend(
            [
                "Issue 正文：",
                issue_body,
            ]
        )
        return "\n".join(lines).strip()

    def build_feishu_handoff_thread_prompt(
        self,
        repo_full_name: str,
        issue_number: int,
    ) -> str:
        session = self.get_issue_session(repo_full_name, issue_number)
        backend = str(session["backend"] or self.config["execution_backend"]) if session else self.config["execution_backend"]
        selected_model = str(session["selected_model"] or "").strip() if session else ""
        _, model_display = resolve_executor_model_display(
            self.config,
            backend,
            selected_model,
        )
        example_model = example_model_for_backend(self.config, backend)
        return textwrap.dedent(
            f"""
            已切换到 {repo_full_name}#{issue_number} 的讨论线程。

            当前模型：{model_display}

            请直接在线程里说明你想先讨论的方案、边界或风险点。
            不需要再手动指定执行器，模型会自动映射到对应引擎。
            如需切换当前 issue 使用的模型，可发送 `/model {example_model}`。
            发送 `/model default` 可恢复到系统默认模型；确认开始执行后，可发送 `/run` 或 `执行方案1`。
            """
        ).strip()

    def build_feishu_reuse_binding_notice(
        self,
        repo_full_name: str,
        issue_number: int,
    ) -> str:
        return textwrap.dedent(
            f"""
            [Coder] 已复用 {repo_full_name}#{issue_number} 现有的讨论线程。

            我刚刚已经把引导消息发回原话题，请直接到该话题继续讨论；确认开始执行后，可发送 `/run` 或 `执行方案1`。
            """
        ).strip()

    def build_feishu_discussion_prompt(
        self,
        repo_full_name: str,
        issue_number: int,
        issue_title: str,
        handoff_prompt: str,
        recent_messages: list[dict[str, Any]],
    ) -> str:
        transcript_lines: list[str] = []
        for message in recent_messages[-8:]:
            content = short_text(str(message.get("content") or "").strip(), 1000)
            if not content:
                continue
            sender_type = str(message.get("sender_type") or "").strip().lower()
            sender = "用户" if sender_type == "user" else "助手"
            transcript_lines.append(f"{sender}: {content}")

        lines = [
            f"你正在继续处理 GitHub Issue #{issue_number} 的飞书讨论阶段。",
            f"仓库：{repo_full_name}",
            f"标题：{issue_title}",
            "",
            "当前要求：",
            "- 现在只讨论方案、边界、改动计划和风险。",
            "- 不要开始 coding，不要假装已经执行。",
            "- 在未进入执行阶段前，绝不能声称“已执行”“已修改文件”“已提交 commit”或“已创建 PR”。",
            "- 不要输出 `result:` / `summary:` / `tests:` / `risks:` 模板。",
            "- 回复直接发给飞书用户，保持简洁明确。",
            "- 如果用户还没有明确发送 `/run`、`执行方案1`、`方案1` 等确认，不要把讨论当成执行确认。",
            "",
            "交接背景：",
            short_text(handoff_prompt, 5000),
            "",
            "线程最近消息：",
            "\n".join(transcript_lines) if transcript_lines else "(无)",
            "",
            "请只输出你要发回飞书线程的正文。",
        ]
        return "\n".join(lines).strip()

    def ensure_issue_handoff_binding(
        self,
        repo_full_name: str,
        issue_number: int,
        issue: dict[str, Any],
        handoff_prompt: str,
        *,
        chat_id: str | None = None,
    ) -> tuple[sqlite3.Row, bool]:
        target_chat_id = str(chat_id or self.primary_feishu_chat_id()).strip()
        existing_binding = self.preferred_issue_binding(repo_full_name, issue_number, chat_id=target_chat_id)
        created_new = existing_binding is None

        if existing_binding is None:
            root_message_id = self.feishu_send_text_message(
                target_chat_id,
                build_feishu_handoff_intro(repo_full_name, issue),
            )
            prompt_message_id = self.feishu_reply_in_thread(
                root_message_id,
                self.build_feishu_handoff_thread_prompt(repo_full_name, issue_number),
            )
            prompt_message = self.feishu_get_message(prompt_message_id)
            thread_id = str(prompt_message.get("thread_id") or "").strip()
            if not thread_id:
                root_message = self.feishu_get_message(root_message_id)
                thread_id = str(root_message.get("thread_id") or "").strip()
            if not thread_id:
                raise RuntimeError("Feishu thread_id is missing after creating handoff thread")
            last_seen_message_id = prompt_message_id
            last_seen_message_time = str(prompt_message.get("create_time") or "")
        else:
            root_message_id = (
                str(existing_binding["root_message_id"] or "").strip()
                or str(existing_binding["prompt_message_id"] or "").strip()
            )
            if not root_message_id:
                raise RuntimeError("Feishu binding exists but root_message_id is missing")
            thread_id = str(existing_binding["thread_id"] or "").strip()
            if not thread_id:
                raise RuntimeError("Feishu binding exists but thread_id is missing")
            prompt_message_id = self.feishu_reply_in_thread(
                root_message_id,
                self.build_feishu_handoff_thread_prompt(repo_full_name, issue_number),
            )
            prompt_message = self.feishu_get_message(prompt_message_id)
            refreshed_thread_id = str(prompt_message.get("thread_id") or "").strip()
            if refreshed_thread_id:
                thread_id = refreshed_thread_id
            last_seen_message_id = prompt_message_id
            last_seen_message_time = str(prompt_message.get("create_time") or "")

        route_session_key = build_feishu_thread_session_key(
            self.config,
            repo_full_name,
            issue_number,
            target_chat_id,
            thread_id,
        )
        binding = self.upsert_feishu_binding(
            chat_id=target_chat_id,
            thread_id=thread_id,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            session_key=route_session_key,
            note="auto handoff thread",
            binding_state="waiting_confirm",
            root_message_id=root_message_id,
            prompt_message_id=prompt_message_id,
            last_seen_message_id=last_seen_message_id,
            last_seen_message_time=last_seen_message_time,
            confirm_message_id="",
            confirm_message_time="",
        )
        return binding, created_new

    def build_issue_payload_from_github(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        owner, repo = repo_full_name.split("/", 1)
        token = get_installation_token(self.config)
        issue = github_request(
            self.config,
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            token=token,
        ).json()
        return {
            "action": "feishu_confirmed",
            "repository": {"full_name": repo_full_name},
            "issue": issue,
        }

    def record_issue_trigger(
        self,
        payload: dict[str, Any],
        reason: str,
        *,
        chat_id: str | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        repo_full_name = payload["repository"]["full_name"]
        issue = payload["issue"]
        issue_number = int(issue["number"])
        issue_title = issue.get("title") or f"Issue #{issue_number}"
        issue_state = issue.get("state") or "open"
        owner, repo = repo_full_name.split("/", 1)

        self.upsert_issue_record(
            repo_full_name,
            issue_number,
            issue_title,
            issue_state,
            last_reason=reason,
        )
        session_row = self.ensure_issue_session(repo_full_name, issue_number, issue_title)
        if backend or model:
            session_row = self.upsert_issue_session(
                repo_full_name,
                issue_number,
                backend=backend,
                selected_model=model,
                clear_selected_model=model is None and backend is not None,
            )
        local_session_key = str(session_row["session_key"])
        handoff_prompt = self.build_handoff_prompt(repo_full_name, issue, local_session_key)
        binding, created_new = self.ensure_issue_handoff_binding(
            repo_full_name,
            issue_number,
            issue,
            handoff_prompt,
            chat_id=chat_id,
        )
        if not created_new and chat_id:
            self.feishu_send_text_message(
                str(chat_id),
                self.build_feishu_reuse_binding_notice(repo_full_name, issue_number),
            )
        session_row = self.upsert_issue_session(
            repo_full_name,
            issue_number,
            session_state="waiting_confirm",
            last_trigger_reason=reason,
            last_triggered_at=now_utc(),
            handoff_prompt=handoff_prompt,
            last_result_status="waiting_confirm",
        )

        token = get_installation_token(self.config)
        comment_issue(
            self.config,
            token,
            owner,
            repo,
            issue_number,
            textwrap.dedent(
                f"""
                {service_actor_name()} 已登记此 Issue，并已{"创建" if created_new else "复用"}飞书讨论线程。

                - Trigger: `{reason}`
                - Session: `{session_row['session_key']}`
                - State: `{session_row['session_state']}`
                - Feishu Chat: `{binding['chat_id']}`
                - Feishu Thread: `{binding['thread_id']}`
                - Confirm Keywords: `{", ".join(self.config['feishu_confirm_keywords'])}`

                请先在线程里讨论方案，确认后在线程中发送 `/run` 或 `执行方案1`。
                """
            ).strip(),
        )
        try:
            set_issue_status_label(
                self.config, token, owner, repo, issue_number,
                "issue_label_accepted_name",
            )
        except Exception as exc:
            print(f"warning: failed to set issue label for {repo_full_name}#{issue_number}: {exc}")
        return {
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
            "session_key": str(session_row["session_key"]),
            "session_state": str(session_row["session_state"]),
            "chat_id": str(binding["chat_id"]),
            "thread_id": str(binding["thread_id"]),
        }

    def create_job(
        self,
        payload: dict[str, Any],
        reason: str,
        *,
        backend: str | None = None,
        selected_model: str | None = None,
    ) -> tuple[str, Path, bool]:
        repo_full_name = payload["repository"]["full_name"]
        issue = payload["issue"]
        issue_number = int(issue["number"])
        issue_title = issue.get("title") or f"Issue #{issue_number}"
        issue_state = issue.get("state") or "open"

        existing = self.get_existing_active_job(repo_full_name, issue_number)
        if existing:
            return str(existing["job_id"]), Path(str(existing["job_dir"])), False

        job_id = f"issue-{issue_number}-{int(time.time() * 1000)}"
        job_path = Path(self.config["job_root"]) / job_id
        job_path.mkdir(parents=True, exist_ok=True)
        execution_backend, execution_model = resolve_model_backend(
            self.config,
            backend=backend,
            selected_model=selected_model,
        )
        execution = {
            "backend": execution_backend,
            "selected_model": str(selected_model or "").strip() or None,
            "model": execution_model,
            "actual_backend": None,
            "actual_model": None,
        }
        payload_with_execution = dict(payload)
        payload_with_execution["_execution"] = execution
        job_data = {
            "job_id": job_id,
            "queued_at": now_utc(),
            "reason": reason,
            "payload": payload_with_execution,
            "execution": execution,
        }
        (job_path / "job.json").write_text(
            json.dumps(job_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        payload_json = json.dumps(payload_with_execution, ensure_ascii=False)
        created_at = now_utc()
        db_module.insert_job(
            self.config,
            job_id=job_id,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            reason=reason,
            payload_json=payload_json,
            status="queued",
            created_at=created_at,
            job_dir=str(job_path),
        )
        db_module.upsert_issue_record(
            self.config,
            repo_full_name,
            issue_number,
            issue_title,
            issue_state,
            active_job_id=job_id,
            last_reason=reason,
            updated_at=created_at,
            closed_at=created_at if issue_state == "closed" else None,
        )
        return job_id, job_path, True

    def fetch_waiting_feishu_bindings(self) -> list[sqlite3.Row]:
        return db_module.fetch_waiting_feishu_bindings(self.config)

    def issue_payload_for_execution(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        return self.build_issue_payload_from_github(repo_full_name, issue_number)

    def confirm_feishu_binding_and_queue(
        self,
        binding: sqlite3.Row,
        confirm_message: dict[str, Any],
    ) -> tuple[str, bool]:
        repo_full_name = str(binding["repo_full_name"])
        issue_number = int(binding["issue_number"])
        issue_payload = self.issue_payload_for_execution(repo_full_name, issue_number)
        issue = issue_payload["issue"]
        issue_title = issue.get("title") or f"Issue #{issue_number}"
        session_snapshot = self.get_issue_session(repo_full_name, issue_number)
        session_backend = (
            str(session_snapshot["backend"] or "").strip().lower()
            if session_snapshot is not None and session_snapshot["backend"]
            else default_backend_name(self.config)
        )
        session_selected_model = (
            str(session_snapshot["selected_model"] or "").strip() or None
            if session_snapshot is not None
            else None
        )
        job_id, _, created = self.create_job(
            issue_payload,
            f"feishu.confirm:{binding['chat_id']}:{binding['thread_id']}",
            backend=session_backend,
            selected_model=session_selected_model,
        )
        session_row = self.upsert_issue_session(
            repo_full_name,
            issue_number,
            session_state="queued",
            last_trigger_reason=f"feishu.confirm:{binding['chat_id']}:{binding['thread_id']}",
            last_triggered_at=now_utc(),
            summary=str(confirm_message.get("content") or "").strip() or None,
            last_result_status="queued",
        )
        self.upsert_feishu_binding(
            chat_id=str(binding["chat_id"]),
            thread_id=str(binding["thread_id"]),
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            session_key=str(binding["session_key"]),
            note=str(binding["note"] or "") or None,
            binding_state="confirmed",
            root_message_id=str(binding["root_message_id"] or "") or None,
            prompt_message_id=str(binding["prompt_message_id"] or "") or None,
            last_seen_message_id=str(confirm_message["message_id"]),
            last_seen_message_time=str(confirm_message["create_time"]),
            confirm_message_id=str(confirm_message["message_id"]),
            confirm_message_time=str(confirm_message["create_time"]),
        )
        owner, repo = repo_full_name.split("/", 1)
        _, model_display = resolve_executor_model_display(
            self.config,
            str(session_row["backend"] or "").strip(),
            str(session_row["selected_model"] or "").strip(),
        )
        if str(binding["root_message_id"] or "").strip():
            try:
                action_text = "已收到确认，开始执行。" if created else "已收到确认，已有执行任务，继续跟踪当前 Job。"
                self.feishu_reply_in_thread(
                    str(binding["root_message_id"]),
                    textwrap.dedent(
                        f"""
                        {action_text}

                        - Issue: `{repo_full_name}#{issue_number}`
                        - Job: `{job_id}`
                        - Model: {model_display}
                        """
                    ).strip(),
                )
            except Exception as exc:
                print(f"warning: failed to reply in Feishu thread for {repo_full_name}#{issue_number}: {exc}")
        try:
            token = get_installation_token(self.config)
            comment_issue(
                self.config,
                token,
                owner,
                repo,
                issue_number,
                textwrap.dedent(
                    f"""
                    {service_actor_name()} 已收到飞书线程确认，开始排队执行。

                    - Issue: `{repo_full_name}#{issue_number}`
                    - Session: `{session_row['session_key']}`
                    - Job: `{job_id}`
                    - Model: {model_display}
                    - Title: `{issue_title}`
                    """
                ).strip(),
            )
            try:
                set_issue_status_label(
                    self.config, token, owner, repo, issue_number,
                    "issue_label_in_progress_name",
                )
            except Exception as exc:
                print(f"warning: failed to set issue label for {repo_full_name}#{issue_number}: {exc}")
        except Exception as exc:
            print(f"warning: failed to post GitHub run acknowledgement for {repo_full_name}#{issue_number}: {exc}")
        return job_id, created

    def reply_issue_progress_to_feishu(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        job_id: str,
        message: str,
        backend: str | None = None,
        selected_model: str | None = None,
        actual_backend: str | None = None,
        actual_model: str | None = None,
    ) -> None:
        try:
            binding = self.preferred_issue_binding(repo_full_name, issue_number)
            if binding is None:
                return
            root_message_id = str(binding["root_message_id"] or "").strip() or str(binding["prompt_message_id"] or "").strip()
            if not root_message_id:
                return
            _, model_display = resolve_executor_model_display(
                self.config,
                backend,
                selected_model,
                actual_backend=actual_backend,
                actual_model=actual_model,
            )
            lines = [
                f"{service_actor_name()} 执行进度：{short_text(message, 180)}",
                f"- Issue: `{repo_full_name}#{issue_number}`",
                f"- Job: `{job_id}`",
                f"- Model: {model_display}",
            ]
            self.feishu_reply_in_thread(root_message_id, "\n".join(lines).strip())
        except Exception as exc:
            print(f"warning: failed to post Feishu progress reply for {repo_full_name}#{issue_number}: {exc}")

    def reply_issue_execution_result_to_feishu(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        job_id: str,
        status: str,
        pr_url: str | None = None,
        result_summary: str | None = None,
        error_text: str | None = None,
        token_usage: dict[str, int] | None = None,
        backend: str | None = None,
        selected_model: str | None = None,
        actual_backend: str | None = None,
        actual_model: str | None = None,
    ) -> None:
        try:
            binding = self.preferred_issue_binding(repo_full_name, issue_number)
            if binding is None:
                print(f"warning: no Feishu binding found for {repo_full_name}#{issue_number}; skip result reply")
                return

            root_message_id = str(binding["root_message_id"] or "").strip() or str(binding["prompt_message_id"] or "").strip()
            if not root_message_id:
                print(
                    f"warning: Feishu binding missing root_message_id for "
                    f"{repo_full_name}#{issue_number}; skip result reply"
                )
                return

            _, model_display = resolve_executor_model_display(
                self.config,
                backend,
                selected_model,
                actual_backend=actual_backend,
                actual_model=actual_model,
            )

            if status == "succeeded":
                lines = [
                    f"{service_actor_name()} 已完成执行。",
                    f"- Issue: `{repo_full_name}#{issue_number}`",
                    f"- Job: `{job_id}`",
                    f"- Model: {model_display}",
                ]
                if token_usage and int(token_usage.get("total_tokens") or 0) > 0:
                    lines.append(f"- Tokens: `{int(token_usage['total_tokens'])}`")
                if pr_url:
                    lines.append(f"- PR: `{pr_url}`")
                if result_summary:
                    lines.extend(["", short_text(result_summary, 3000)])
            elif status == "no_change":
                lines = [
                    f"{service_actor_name()} 已完成（无改动）。",
                    f"- Issue: `{repo_full_name}#{issue_number}`",
                    f"- Job: `{job_id}`",
                    f"- Model: {model_display}",
                ]
                if token_usage and int(token_usage.get("total_tokens") or 0) > 0:
                    lines.append(f"- Tokens: `{int(token_usage['total_tokens'])}`")
                if result_summary:
                    lines.extend(["", short_text(result_summary, 3000)])
            else:
                summary = short_text(error_text or result_summary or "(no error text)", 700)
                lines = [
                    f"{service_actor_name()} 执行失败。",
                    f"- Issue: `{repo_full_name}#{issue_number}`",
                    f"- Job: `{job_id}`",
                    f"- Model: {model_display}",
                ]
                if token_usage and int(token_usage.get("total_tokens") or 0) > 0:
                    lines.append(f"- Tokens: `{int(token_usage['total_tokens'])}`")
                lines.extend(["", "错误摘要：", summary])

            self.feishu_reply_in_thread(root_message_id, "\n".join(lines).strip())
        except Exception as exc:
            print(f"warning: failed to post Feishu result reply for {repo_full_name}#{issue_number}: {exc}")

    def reply_issue_discussion_to_feishu(
        self,
        repo_full_name: str,
        issue_number: int,
        *,
        binding: sqlite3.Row,
        recent_messages: list[dict[str, Any]],
    ) -> str | None:
        issue_row = self.fetchone(
            """
            SELECT issue_title
            FROM issues
            WHERE repo_full_name = ? AND issue_number = ?
            """,
            (repo_full_name, issue_number),
        )
        issue_title = str(issue_row["issue_title"]) if issue_row and issue_row["issue_title"] else f"Issue #{issue_number}"
        session_row = self.ensure_issue_session(repo_full_name, issue_number, issue_title)
        if not session_allows_discussion_reply(str(row_get(session_row, "session_state", "waiting_confirm") or "")):
            return str(row_get(session_row, "agent_session_id", "") or "").strip() or None
        handoff_prompt = str(session_row["handoff_prompt"] or "").strip()
        if not handoff_prompt:
            return None

        latest_user_message = recent_messages[-1] if recent_messages else None
        model_command = parse_feishu_model_command(str((latest_user_message or {}).get("content") or ""))
        if model_command is not None:
            if model_command and backend_for_model(self.config, model_command) is None:
                allowed_models = ", ".join(supported_models_union(self.config))
                response_text = (
                    f"[Coder] 不支持模型 `{model_command}`。\n\n"
                    f"当前可用模型：{allowed_models}\n"
                    "模型会自动映射到对应引擎；发送 `/model default` 可恢复到系统默认模型。"
                )
            else:
                selected_model = model_command or None
                next_backend = (
                    backend_for_model(self.config, selected_model)
                    if selected_model
                    else default_backend_name(self.config)
                )
                session_row = self.upsert_issue_session(
                    repo_full_name,
                    issue_number,
                    backend=next_backend,
                    selected_model=selected_model,
                    clear_selected_model=selected_model is None,
                    clear_agent_session_id=(
                        selected_model is None
                        and next_backend != str(session_row["backend"] or self.config["execution_backend"]).strip().lower()
                    ),
                    summary=(
                        f"selected model: {selected_model}"
                        if selected_model
                        else "selected model reset to default"
                    ),
                    last_result_status="waiting_confirm",
                )
                _, model_display = resolve_executor_model_display(
                    self.config,
                    str(session_row["backend"] or "").strip(),
                    str(session_row["selected_model"] or "").strip(),
                )
                response_text = (
                    f"[Coder] 当前 issue 模型已设置为 {model_display}。"
                    if selected_model
                    else f"[Coder] 当前 issue 已恢复使用默认模型 {model_display}。"
                )

            root_message_id = str(binding["root_message_id"] or "").strip() or str(binding["prompt_message_id"] or "").strip()
            if not root_message_id:
                raise RuntimeError(f"Feishu binding missing root_message_id for {repo_full_name}#{issue_number}")
            self.feishu_reply_in_thread(root_message_id, response_text)
            return str(session_row["agent_session_id"] or "").strip() or None

        prompt = self.build_feishu_discussion_prompt(
            repo_full_name,
            issue_number,
            issue_title,
            handoff_prompt,
            recent_messages,
        )
        discussion_dir = worker_module.repo_checkout_dir(self.config, repo_full_name, issue_number)
        if not (discussion_dir / ".git").exists():
            discussion_dir = worker_module.repo_issue_root(self.config, repo_full_name, issue_number)
            discussion_dir.mkdir(parents=True, exist_ok=True)
        turn = worker_module.run_discussion_turn(
            self.config,
            repo_full_name,
            issue_number,
            discussion_dir,
            prompt,
            str(session_row["session_key"]),
            agent_session_id=str(session_row["agent_session_id"] or "").strip() or None,
            selected_model=str(session_row["selected_model"] or "").strip() or None,
            backend=str(session_row["backend"] or "").strip() or None,
        )
        response_text = str(turn["text"])
        agent_session_id = str(turn["agent_session_id"] or "").strip() or None

        root_message_id = str(binding["root_message_id"] or "").strip() or str(binding["prompt_message_id"] or "").strip()
        if not root_message_id:
            raise RuntimeError(f"Feishu binding missing root_message_id for {repo_full_name}#{issue_number}")
        current_session = self.get_issue_session(repo_full_name, issue_number)
        if current_session is not None and not session_allows_discussion_reply(
            str(row_get(current_session, "session_state", "") or "")
        ):
            return agent_session_id
        self.feishu_reply_in_thread(root_message_id, response_text)
        self.upsert_issue_session(
            repo_full_name,
            issue_number,
            session_state="bound",
            agent_session_id=agent_session_id,
            summary=response_text,
            last_result_status="waiting_confirm",
        )
        return agent_session_id

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return db_module.get_job(self.config, job_id)

    def job_payload(self, job_id: str) -> dict[str, Any] | None:
        row = self.get_job(job_id)
        if row is None:
            return None
        return self.job_payload_from_row(row)

    def job_payload_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        job_dir = str(row["job_dir"] or "").strip()
        execution: dict[str, Any] | None = None
        try:
            payload_data = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError:
            payload_data = {}
        if isinstance(payload_data, dict):
            raw_execution = payload_data.get("_execution")
            if isinstance(raw_execution, dict):
                execution = raw_execution
        if job_dir:
            job_file = Path(job_dir) / "job.json"
            if job_file.is_file():
                try:
                    job_data = json.loads(job_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    job_data = {}
                if isinstance(job_data, dict) and isinstance(job_data.get("execution"), dict):
                    execution = dict(job_data["execution"])
            payload["token_usage"] = worker_module.read_job_token_usage(Path(job_dir))
        else:
            payload["token_usage"] = None
        payload["execution"] = execution
        return payload

    def mark_job_running(self, job_id: str, pid: int) -> None:
        db_module.mark_job_running(self.config, job_id, pid)

    def mark_job_finished(
        self,
        job_id: str,
        status: str,
        *,
        error_text: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        db_module.mark_job_finished(
            self.config,
            job_id,
            status,
            error_text=error_text,
            result_summary=result_summary,
        )

    def requeue_job(self, job_id: str, error_text: str | None = None) -> None:
        db_module.requeue_job(self.config, job_id, error_text)

    def cleanup_closed_issue_if_finished(self, repo_full_name: str, issue_number: int) -> None:
        issue_row = self.fetchone(
            "SELECT issue_state FROM issues WHERE repo_full_name = ? AND issue_number = ?",
            (repo_full_name, issue_number),
        )
        if not issue_row or issue_row["issue_state"] != "closed":
            return
        active = self.fetchone(
            "SELECT job_id FROM jobs WHERE repo_full_name = ? AND issue_number = ? AND status IN ('queued', 'running') LIMIT 1",
            (repo_full_name, issue_number),
        )
        if active:
            return
        self.execute(
            "DELETE FROM jobs WHERE repo_full_name = ? AND issue_number = ?",
            (repo_full_name, issue_number),
        )
        self.execute(
            "DELETE FROM issues WHERE repo_full_name = ? AND issue_number = ?",
            (repo_full_name, issue_number),
        )
        from src import scheduler as scheduler_module

        scheduler_module.remove_issue_from_state(self.config, repo_full_name, issue_number)
        issue_root = worker_module.repo_issue_root(self.config, repo_full_name, issue_number)
        if issue_root.exists():
            import shutil

            shutil.rmtree(issue_root, ignore_errors=True)
        self.execute(
            "DELETE FROM feishu_bindings WHERE repo_full_name = ? AND issue_number = ?",
            (repo_full_name, issue_number),
        )

    def handle_issue_closed(self, repo_full_name: str, issue: dict[str, Any]) -> None:
        issue_number = int(issue["number"])
        self.upsert_issue_record(
            repo_full_name,
            issue_number,
            issue.get("title") or f"Issue #{issue_number}",
            "closed",
        )
        self.upsert_issue_session(
            repo_full_name,
            issue_number,
            session_state="closed",
            last_result_status="closed",
        )
        self.execute(
            """
            UPDATE jobs
            SET status = 'cancelled', finished_at = ?, error_text = 'issue closed before execution'
            WHERE repo_full_name = ? AND issue_number = ? AND status = 'queued'
            """,
            (now_utc(), repo_full_name, issue_number),
        )
        self.cleanup_closed_issue_if_finished(repo_full_name, issue_number)

    def issue_session_payload(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        session_row = self.get_issue_session(repo_full_name, issue_number)
        issue_row = self.fetchone(
            """
            SELECT issue_title, issue_state, active_job_id, last_reason, updated_at, closed_at
            FROM issues
            WHERE repo_full_name = ? AND issue_number = ?
            """,
            (repo_full_name, issue_number),
        )
        bindings = self.list_issue_bindings(repo_full_name, issue_number)
        jobs = self.list_issue_jobs(repo_full_name, issue_number)
        return {
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
            "issue": dict(issue_row) if issue_row else None,
            "session": dict(session_row) if session_row else None,
            "paths": {
                "issue_root": str(worker_module.repo_issue_root(self.config, repo_full_name, issue_number)),
                "repo_path": str(worker_module.repo_checkout_dir(self.config, repo_full_name, issue_number)),
            },
            "bindings": [dict(row) for row in bindings],
            "binding_count": len(bindings),
            "jobs": [self.job_payload_from_row(row) for row in jobs],
            "job_count": len(jobs),
        }


__all__ = ["IssueService"]
