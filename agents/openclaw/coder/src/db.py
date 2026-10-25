from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from src.utils.helpers import ensure_dir, now_utc


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_state TEXT NOT NULL,
    active_job_id TEXT,
    last_reason TEXT,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (repo_full_name, issue_number)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    worker_pid INTEGER,
    error_text TEXT,
    result_summary TEXT,
    job_dir TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_repo_created
ON jobs (status, repo_full_name, created_at);

CREATE INDEX IF NOT EXISTS idx_jobs_repo_issue_created
ON jobs (repo_full_name, issue_number, created_at);

CREATE TABLE IF NOT EXISTS issue_sessions (
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    backend TEXT NOT NULL,
    selected_model TEXT,
    session_key TEXT NOT NULL,
    session_state TEXT NOT NULL,
    last_trigger_reason TEXT,
    last_triggered_at TEXT,
    handoff_prompt TEXT,
    agent_session_id TEXT,
    branch_name TEXT NOT NULL,
    pr_url TEXT,
    summary TEXT,
    last_result_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_full_name, issue_number)
);

CREATE INDEX IF NOT EXISTS idx_issue_sessions_backend_updated
ON issue_sessions (backend, updated_at);

CREATE TABLE IF NOT EXISTS feishu_bindings (
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    binding_state TEXT NOT NULL,
    note TEXT,
    root_message_id TEXT,
    prompt_message_id TEXT,
    last_seen_message_id TEXT,
    last_seen_message_time TEXT,
    confirm_message_id TEXT,
    confirm_message_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_feishu_bindings_issue
ON feishu_bindings (repo_full_name, issue_number, updated_at);
"""


REQUIRED_COLUMNS = {
    "deliveries": {"delivery_id", "event_name", "received_at"},
    "issues": {
        "repo_full_name",
        "issue_number",
        "issue_title",
        "issue_state",
        "active_job_id",
        "last_reason",
        "updated_at",
        "closed_at",
    },
    "jobs": {
        "job_id",
        "repo_full_name",
        "issue_number",
        "reason",
        "payload_json",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "worker_pid",
        "error_text",
        "result_summary",
        "job_dir",
    },
    "issue_sessions": {
        "repo_full_name",
        "issue_number",
        "backend",
        "selected_model",
        "session_key",
        "session_state",
        "last_trigger_reason",
        "last_triggered_at",
        "handoff_prompt",
        "agent_session_id",
        "branch_name",
        "pr_url",
        "summary",
        "last_result_status",
        "created_at",
        "updated_at",
    },
    "feishu_bindings": {
        "chat_id",
        "thread_id",
        "repo_full_name",
        "issue_number",
        "session_key",
        "binding_state",
        "note",
        "root_message_id",
        "prompt_message_id",
        "last_seen_message_id",
        "last_seen_message_time",
        "confirm_message_id",
        "confirm_message_time",
        "created_at",
        "updated_at",
    },
}

OPTIONAL_COLUMN_DEFINITIONS = {
    "issue_sessions": {
        "selected_model": "TEXT",
    },
}

def db_connect(config: dict[str, Any]) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(Path(config["db_path"])),
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_columns(connection: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]): row for row in rows}


def validate_db_schema(config: dict[str, Any]) -> None:
    with db_connect(config) as connection:
        for table_name, required_columns in REQUIRED_COLUMNS.items():
            current_columns = set(table_columns(connection, table_name).keys())
            if not current_columns:
                raise RuntimeError(f"missing required table: {table_name}")
            missing = sorted(required_columns - current_columns)
            if missing:
                raise RuntimeError(
                    f"table `{table_name}` is missing required columns: {', '.join(missing)}"
                )


def ensure_optional_columns(connection: sqlite3.Connection) -> None:
    for table_name, definitions in OPTIONAL_COLUMN_DEFINITIONS.items():
        current_columns = set(table_columns(connection, table_name).keys())
        for column_name, column_sql in definitions.items():
            if column_name in current_columns:
                continue
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def init_db(config: dict[str, Any]) -> None:
    ensure_dir(Path(config["db_path"]).parent)
    with db_connect(config) as connection:
        connection.executescript(SCHEMA_SQL)
        ensure_optional_columns(connection)
    validate_db_schema(config)


def fetchone(config: dict[str, Any], query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_connect(config) as connection:
        return connection.execute(query, params).fetchone()


def fetchall(config: dict[str, Any], query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_connect(config) as connection:
        return list(connection.execute(query, params).fetchall())


def execute(config: dict[str, Any], query: str, params: tuple[Any, ...] = ()) -> None:
    with db_connect(config) as connection:
        connection.execute(query, params)


def record_delivery_once(config: dict[str, Any], delivery_id: str, event_name: str) -> bool:
    received_at = now_utc()
    with db_connect(config) as connection:
        existing = connection.execute(
            "SELECT delivery_id FROM deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if existing:
            return False
        connection.execute(
            "INSERT INTO deliveries (delivery_id, event_name, received_at) VALUES (?, ?, ?)",
            (delivery_id, event_name, received_at),
        )
        connection.execute(
            """
            DELETE FROM deliveries
            WHERE delivery_id NOT IN (
                SELECT delivery_id FROM deliveries ORDER BY received_at DESC LIMIT 5000
            )
            """
        )
        return True


def upsert_issue_record(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    issue_title: str,
    issue_state: str,
    *,
    active_job_id: str | None = None,
    last_reason: str | None = None,
    updated_at: str | None = None,
    closed_at: str | None = None,
) -> None:
    now = updated_at or now_utc()
    final_closed_at = closed_at if closed_at is not None else (now if issue_state == "closed" else None)
    with db_connect(config) as connection:
        connection.execute(
            """
            INSERT INTO issues (
                repo_full_name, issue_number, issue_title, issue_state,
                active_job_id, last_reason, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_full_name, issue_number) DO UPDATE SET
                issue_title = excluded.issue_title,
                issue_state = excluded.issue_state,
                active_job_id = COALESCE(excluded.active_job_id, issues.active_job_id),
                last_reason = COALESCE(excluded.last_reason, issues.last_reason),
                updated_at = excluded.updated_at,
                closed_at = excluded.closed_at
            """,
            (
                repo_full_name,
                issue_number,
                issue_title,
                issue_state,
                active_job_id,
                last_reason,
                now,
                final_closed_at,
            ),
        )


def clear_issue_active_job(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    job_id: str,
) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            UPDATE issues
            SET active_job_id = NULL, updated_at = ?
            WHERE repo_full_name = ? AND issue_number = ? AND active_job_id = ?
            """,
            (now_utc(), repo_full_name, issue_number, job_id),
        )


def get_existing_active_job(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    statuses: tuple[str, ...],
) -> sqlite3.Row | None:
    if not statuses:
        return None
    placeholders = ", ".join("?" for _ in statuses)
    with db_connect(config) as connection:
        return connection.execute(
            f"""
            SELECT job_id, job_dir, status
            FROM jobs
            WHERE repo_full_name = ? AND issue_number = ? AND status IN ({placeholders})
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (repo_full_name, issue_number, *statuses),
        ).fetchone()


def get_issue_session(config: dict[str, Any], repo_full_name: str, issue_number: int) -> sqlite3.Row | None:
    with db_connect(config) as connection:
        return connection.execute(
            """
            SELECT *
            FROM issue_sessions
            WHERE repo_full_name = ? AND issue_number = ?
            """,
            (repo_full_name, issue_number),
        ).fetchone()


def upsert_issue_session(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    *,
    backend: str,
    selected_model: str | None,
    session_key: str,
    session_state: str,
    last_trigger_reason: str | None,
    last_triggered_at: str | None,
    handoff_prompt: str | None,
    agent_session_id: str | None,
    branch_name: str,
    pr_url: str | None,
    summary: str | None,
    last_result_status: str | None,
    created_at: str,
    updated_at: str,
) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            INSERT INTO issue_sessions (
                repo_full_name, issue_number, backend, selected_model, session_key, session_state,
                last_trigger_reason, last_triggered_at, handoff_prompt, agent_session_id,
                branch_name, pr_url, summary, last_result_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_full_name, issue_number) DO UPDATE SET
                backend = excluded.backend,
                selected_model = excluded.selected_model,
                session_key = excluded.session_key,
                session_state = excluded.session_state,
                last_trigger_reason = excluded.last_trigger_reason,
                last_triggered_at = excluded.last_triggered_at,
                handoff_prompt = excluded.handoff_prompt,
                agent_session_id = excluded.agent_session_id,
                branch_name = excluded.branch_name,
                pr_url = excluded.pr_url,
                summary = excluded.summary,
                last_result_status = excluded.last_result_status,
                updated_at = excluded.updated_at
            """,
            (
                repo_full_name,
                issue_number,
                backend,
                selected_model,
                session_key,
                session_state,
                last_trigger_reason,
                last_triggered_at,
                handoff_prompt,
                agent_session_id,
                branch_name,
                pr_url,
                summary,
                last_result_status,
                created_at,
                updated_at,
            ),
        )


def get_feishu_binding(config: dict[str, Any], chat_id: str, thread_id: str) -> sqlite3.Row | None:
    with db_connect(config) as connection:
        return connection.execute(
            """
            SELECT *
            FROM feishu_bindings
            WHERE chat_id = ? AND thread_id = ?
            """,
            (chat_id, thread_id),
        ).fetchone()


def list_issue_bindings(config: dict[str, Any], repo_full_name: str, issue_number: int) -> list[sqlite3.Row]:
    with db_connect(config) as connection:
        return list(
            connection.execute(
                """
                SELECT *
                FROM feishu_bindings
                WHERE repo_full_name = ? AND issue_number = ?
                ORDER BY updated_at DESC
                """,
                (repo_full_name, issue_number),
            ).fetchall()
        )


def upsert_feishu_binding(
    config: dict[str, Any],
    *,
    chat_id: str,
    thread_id: str,
    repo_full_name: str,
    issue_number: int,
    session_key: str,
    binding_state: str,
    note: str | None,
    root_message_id: str | None,
    prompt_message_id: str | None,
    last_seen_message_id: str | None,
    last_seen_message_time: str | None,
    confirm_message_id: str | None,
    confirm_message_time: str | None,
    created_at: str,
    updated_at: str,
) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            INSERT INTO feishu_bindings (
                chat_id, thread_id, repo_full_name, issue_number, session_key,
                binding_state, note, root_message_id, prompt_message_id,
                last_seen_message_id, last_seen_message_time, confirm_message_id,
                confirm_message_time, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                repo_full_name = excluded.repo_full_name,
                issue_number = excluded.issue_number,
                session_key = excluded.session_key,
                binding_state = excluded.binding_state,
                note = excluded.note,
                root_message_id = excluded.root_message_id,
                prompt_message_id = excluded.prompt_message_id,
                last_seen_message_id = excluded.last_seen_message_id,
                last_seen_message_time = excluded.last_seen_message_time,
                confirm_message_id = excluded.confirm_message_id,
                confirm_message_time = excluded.confirm_message_time,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                thread_id,
                repo_full_name,
                issue_number,
                session_key,
                binding_state,
                note,
                root_message_id,
                prompt_message_id,
                last_seen_message_id,
                last_seen_message_time,
                confirm_message_id,
                confirm_message_time,
                created_at,
                updated_at,
            ),
        )


def delete_feishu_binding(config: dict[str, Any], chat_id: str, thread_id: str) -> bool:
    with db_connect(config) as connection:
        existing = connection.execute(
            "SELECT chat_id FROM feishu_bindings WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        ).fetchone()
        if existing is None:
            return False
        connection.execute(
            "DELETE FROM feishu_bindings WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id),
        )
        return True


def insert_job(
    config: dict[str, Any],
    *,
    job_id: str,
    repo_full_name: str,
    issue_number: int,
    reason: str,
    payload_json: str,
    status: str,
    created_at: str,
    job_dir: str,
) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, repo_full_name, issue_number, reason, payload_json,
                status, created_at, job_dir
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                repo_full_name,
                issue_number,
                reason,
                payload_json,
                status,
                created_at,
                job_dir,
            ),
        )


def get_job(config: dict[str, Any], job_id: str) -> sqlite3.Row | None:
    with db_connect(config) as connection:
        return connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def list_jobs_for_issue(config: dict[str, Any], repo_full_name: str, issue_number: int) -> list[sqlite3.Row]:
    with db_connect(config) as connection:
        return list(
            connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE repo_full_name = ? AND issue_number = ?
                ORDER BY created_at DESC
                """,
                (repo_full_name, issue_number),
            ).fetchall()
        )


def mark_job_running(config: dict[str, Any], job_id: str, pid: int) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = 'running', started_at = ?, worker_pid = ?, finished_at = NULL, error_text = NULL
            WHERE job_id = ?
            """,
            (now_utc(), pid, job_id),
        )


def mark_job_finished(
    config: dict[str, Any],
    job_id: str,
    status: str,
    *,
    error_text: str | None = None,
    result_summary: str | None = None,
) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, worker_pid = NULL, error_text = ?, result_summary = ?
            WHERE job_id = ?
            """,
            (
                status,
                now_utc(),
                error_text,
                result_summary,
                job_id,
            ),
        )


def requeue_job(config: dict[str, Any], job_id: str, error_text: str | None = None) -> None:
    with db_connect(config) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = 'queued', started_at = NULL, finished_at = NULL, worker_pid = NULL, error_text = ?
            WHERE job_id = ?
            """,
            (error_text, job_id),
        )


def fetch_waiting_feishu_bindings(config: dict[str, Any]) -> list[sqlite3.Row]:
    with db_connect(config) as connection:
        return list(
            connection.execute(
                """
                SELECT *
                FROM feishu_bindings
                WHERE binding_state IN ('waiting_confirm', 'bound', 'confirmed')
                ORDER BY updated_at ASC
                """
            ).fetchall()
        )
