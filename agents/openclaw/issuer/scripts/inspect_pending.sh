#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PENDING_DB_PATH="${PENDING_DB_PATH:-${WORKSPACE_DIR}/state/pending-actions.sqlite3}"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") summary
  $(basename "$0") list [--repo <repo>] [--conversation-id <id>] [--requester-id <id>]
  $(basename "$0") conversation --conversation-id <id>
  $(basename "$0") requester --requester-id <id>
  $(basename "$0") show --draft-id <id>

Examples:
  $(basename "$0") summary
  $(basename "$0") list
  $(basename "$0") list --repo yeying-community/agent
  $(basename "$0") conversation --conversation-id chat:oc_xxx
  $(basename "$0") requester --requester-id ou_xxx
  $(basename "$0") show --draft-id 5d496c27
EOF
}

fail_missing_db() {
  echo "[issuer] ERROR: pending sqlite not found: ${PENDING_DB_PATH}" >&2
  echo "[issuer] hint: start the service once, or confirm WORKSPACE_DIR / PENDING_DB_PATH is correct" >&2
  exit 1
}

[[ -f "${PENDING_DB_PATH}" ]] || fail_missing_db

COMMAND="${1:-summary}"
shift || true

REPO_QUERY=""
CONVERSATION_ID=""
REQUESTER_ID=""
DRAFT_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_QUERY="${2:-}"
      shift 2
      ;;
    --conversation-id)
      CONVERSATION_ID="${2:-}"
      shift 2
      ;;
    --requester-id)
      REQUESTER_ID="${2:-}"
      shift 2
      ;;
    --draft-id)
      DRAFT_ID="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[issuer] ERROR: unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

python3 - "$COMMAND" "$PENDING_DB_PATH" "$REPO_QUERY" "$CONVERSATION_ID" "$REQUESTER_ID" "$DRAFT_ID" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

command, db_path, repo_query, conversation_id, requester_id, draft_id = sys.argv[1:7]


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def print_rows(rows):
    rows = list(rows)
    if not rows:
      print("(no rows)")
      return

    columns = list(rows[0].keys())
    widths = {col: len(col) for col in columns}
    rendered = []
    for row in rows:
        rendered_row = {}
        for col in columns:
            value = row[col]
            text = "" if value is None else str(value)
            rendered_row[col] = text
            widths[col] = max(widths[col], len(text))
        rendered.append(rendered_row)

    header = "  ".join(col.ljust(widths[col]) for col in columns)
    sep = "  ".join("-" * widths[col] for col in columns)
    print(header)
    print(sep)
    for row in rendered:
        print("  ".join(row[col].ljust(widths[col]) for col in columns))


def query_entries(db, where=None, args=()):
    sql = """
    select
      draft_id,
      status,
      slot_key,
      scope_conversation_id,
      requester_id,
      requester_label,
      target_repo_key,
      target_issue_number,
      kind,
      headline,
      updated_at
    from pending_actions
    """
    if where:
        sql += f" where {where}"
    sql += " order by updated_at desc, created_at desc"
    return db.execute(sql, args).fetchall()


db = connect(db_path)

if command == "summary":
    total = db.execute("select count(*) from pending_actions").fetchone()[0]
    active = db.execute("select count(*) from pending_actions where status = 'pending'").fetchone()[0]
    by_status = db.execute(
        """
        select status, count(*) as count
        from pending_actions
        group by status
        order by count desc, status asc
        """
    ).fetchall()
    by_repo = db.execute(
        """
        select target_repo_key as repo, count(*) as count
        from pending_actions
        where status = 'pending'
        group by target_repo_key
        order by count desc, repo asc
        """
    ).fetchall()
    by_conversation = db.execute(
        """
        select scope_conversation_id as conversation_id, count(*) as count
        from pending_actions
        where status = 'pending'
        group by scope_conversation_id
        order by count desc, conversation_id asc
        """
    ).fetchall()
    latest = db.execute(
        """
        select draft_id, status, requester_id, target_repo_key, kind, headline, updated_at
        from pending_actions
        order by updated_at desc, created_at desc
        limit 10
        """
    ).fetchall()

    print(f"db_path={db_path}")
    print(f"total_rows={total}")
    print(f"active_pending={active}")
    print("")
    print("== by status ==")
    print_rows(by_status)
    print("")
    print("== by repo ==")
    print_rows(by_repo)
    print("")
    print("== by conversation ==")
    print_rows(by_conversation)
    print("")
    print("== latest 10 ==")
    print_rows(latest)
    raise SystemExit(0)

if command == "list":
    clauses = []
    args = []
    if repo_query:
        clauses.append("target_repo_key = ?")
        args.append(repo_query.lower())
    if conversation_id:
        clauses.append("scope_conversation_id = ?")
        args.append(conversation_id)
    if requester_id:
        clauses.append("(requester_id = ? or requester_label = ?)")
        args.extend([requester_id, requester_id])
    where = " and ".join(clauses) if clauses else None
    print_rows(query_entries(db, where, tuple(args)))
    raise SystemExit(0)

if command == "conversation":
    if not conversation_id:
        raise SystemExit("conversation command requires --conversation-id")
    print_rows(query_entries(db, "scope_conversation_id = ?", (conversation_id,)))
    raise SystemExit(0)

if command == "requester":
    if not requester_id:
        raise SystemExit("requester command requires --requester-id")
    print_rows(query_entries(db, "(requester_id = ? or requester_label = ?)", (requester_id, requester_id)))
    raise SystemExit(0)

if command == "show":
    if not draft_id:
        raise SystemExit("show command requires --draft-id")

    row = db.execute(
        """
        select *
        from pending_actions
        where lower(draft_id) = lower(?)
           or lower(draft_id) like lower(?)
        order by updated_at desc, created_at desc
        limit 1
        """,
        (draft_id, f"{draft_id}%"),
    ).fetchone()
    if row is None:
        print("(draft not found)")
        raise SystemExit(1)

    print("== row ==")
    print_rows([row])
    print("")
    print("== payload_json ==")
    try:
        payload = json.loads(row["payload_json"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        print(row["payload_json"])
    raise SystemExit(0)

raise SystemExit(f"unknown command: {command}")
PY
