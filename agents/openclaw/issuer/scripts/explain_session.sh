#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SESSIONS_DIR="${SESSIONS_DIR:-${STATE_DIR}/agents/main/sessions}"
SESSIONS_INDEX="${SESSIONS_INDEX:-${SESSIONS_DIR}/sessions.json}"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") [last]
  $(basename "$0") <session-id-or-prefix>
  $(basename "$0") --file <trajectory.jsonl>

Examples:
  $(basename "$0")
  $(basename "$0") last
  $(basename "$0") 97aa7070
  $(basename "$0") --file ${SESSIONS_DIR}/97aa7070-88f2-4a59-9be1-6a05c6ac3459.trajectory.jsonl
EOF
}

TARGET_MODE="last"
TARGET_VALUE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    last)
      TARGET_MODE="last"
      TARGET_VALUE=""
      shift
      ;;
    --file)
      TARGET_MODE="file"
      TARGET_VALUE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      TARGET_MODE="session"
      TARGET_VALUE="$1"
      shift
      ;;
  esac
done

python3 - "$TARGET_MODE" "$TARGET_VALUE" "$SESSIONS_DIR" "$SESSIONS_INDEX" <<'PY'
import glob
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

target_mode, target_value, sessions_dir_raw, sessions_index_raw = sys.argv[1:5]
sessions_dir = Path(sessions_dir_raw)
sessions_index = Path(sessions_index_raw)


def fail(message: str) -> None:
    print(f"[issuer] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def shorten(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def first_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text", ""))
    return ""


def first_tool_call(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "toolCall":
            return item
    return None


def summarize_user_prompt(prompt_text: str) -> str:
    lines = [line.strip() for line in str(prompt_text or "").splitlines()]
    candidates = []
    for line in lines:
        if not line:
            continue
        if line.startswith("[message_id:"):
            continue
        if line.startswith("[System:"):
            continue
        if line.startswith("Reply target of current user message"):
            break
        if line.startswith("```"):
            continue
        candidates.append(line)

    for line in candidates:
        if line.startswith("/"):
            return shorten(line, 140)

    for line in candidates:
        if ": " in line:
            _, remainder = line.split(": ", 1)
            remainder = remainder.strip()
            if not remainder or remainder.startswith("[Replying to:"):
                continue
            return shorten(remainder, 140)

    for line in candidates:
        if line.startswith("[Replying to:"):
            continue
        return shorten(line, 140)

    return shorten(str(prompt_text or "").splitlines()[0] if prompt_text else "(unknown user input)", 140)


def summarize_tool_result(tool_name: str, text: str) -> str:
    if not text:
        return "no text result"

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            if tool_name == "exec":
                mode = payload.get("mode")
                action = payload.get("action")
                owner = payload.get("owner")
                repo = payload.get("repo")
                pending = payload.get("pending") or {}
                draft_id = pending.get("draftId")
                issue = payload.get("issue") or payload.get("result", {}).get("issue") or {}
                parts = []
                if mode:
                    parts.append(f"mode={mode}")
                if action:
                    parts.append(f"action={action}")
                if owner and repo:
                    parts.append(f"repo={owner}/{repo}")
                if draft_id:
                    parts.append(f"draft={str(draft_id)[:8]}")
                if issue.get("number"):
                    parts.append(f"issue=#{issue['number']}")
                if not parts and payload.get("ok") is not None:
                    parts.append(f"ok={payload['ok']}")
                return ", ".join(parts) or shorten(stripped, 100)
            return shorten(stripped, 100)

    first_line = stripped.splitlines()[0]
    return shorten(first_line, 100)


def summarize_command(command: str) -> str:
    command = str(command or "").strip()
    if not command:
        return "(empty command)"

    try:
        parts = shlex.split(command)
    except Exception:
        return shorten(command, 120)

    summary = []
    script = ""
    args = parts[:]
    if parts and parts[0] == "node" and len(parts) >= 2:
        script = parts[1]
        args = parts[2:]
    elif parts:
        script = parts[0]
        args = parts[1:]

    if script:
        summary.append(script)

    important_flags = {"--action", "--kind", "--owner", "--repo", "--issue", "--issueUrl", "--execute"}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in important_flags:
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                summary.append(f"{arg} {args[i + 1]}")
                i += 2
                continue
            summary.append(arg)
        i += 1

    return shorten(" ".join(summary), 140)


def resolve_trajectory_path() -> Path:
    if target_mode == "file":
        if not target_value:
            fail("--file requires a path")
        path = Path(target_value).expanduser()
        if not path.exists():
            fail(f"trajectory file not found: {path}")
        return path

    if target_mode == "session":
        if not target_value:
            fail("missing session id")
        candidates = sorted(glob.glob(str(sessions_dir / f"{target_value}*.trajectory.jsonl")))
        if len(candidates) == 1:
            return Path(candidates[0])
        if len(candidates) > 1:
            fail(f"multiple trajectory files matched prefix {target_value}: {', '.join(Path(p).name for p in candidates[:5])}")

        exact = sessions_dir / f"{target_value}.trajectory.jsonl"
        if exact.exists():
            return exact
        fail(f"no trajectory file matched session id/prefix: {target_value}")

    if not sessions_index.exists():
        fail(f"sessions index not found: {sessions_index}")

    payload = json.loads(sessions_index.read_text(encoding="utf-8"))
    items: list[tuple[int, Path]] = []
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        session_file = str(entry.get("sessionFile") or "").strip()
        updated_at = int(entry.get("updatedAt") or 0)
        if not session_file:
            continue
        base = Path(session_file)
        trajectory = base.with_suffix(".trajectory.jsonl")
        if trajectory.exists():
            items.append((updated_at, trajectory))

    if not items:
        fail(f"no trajectory files found under {sessions_dir}")

    items.sort(key=lambda item: item[0], reverse=True)
    return items[0][1]


@dataclass
class ToolStep:
    name: str
    summary: str
    result: str


def extract_runs(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    started = {}
    completed = {}
    compiled = {}
    artifacts = {}
    for line in lines:
        line_type = line.get("type")
        run_id = line.get("runId")
        if not run_id:
            continue
        if line_type == "session.started":
            started[run_id] = line
        elif line_type == "context.compiled":
            compiled[run_id] = line
        elif line_type == "model.completed":
            completed[run_id] = line
        elif line_type == "trace.artifacts":
            artifacts[run_id] = line

    ordered = []
    for run_id, item in sorted(started.items(), key=lambda pair: pair[1].get("ts", "")):
        ordered.append(
            {
                "run_id": run_id,
                "started": item,
                "compiled": compiled.get(run_id),
                "completed": completed.get(run_id),
                "artifacts": artifacts.get(run_id),
            }
        )
    return ordered


def locate_current_slice(messages: list[dict[str, Any]], final_prompt_text: str) -> list[dict[str, Any]]:
    if not messages:
        return []

    target = str(final_prompt_text or "").strip()
    if not target:
        return messages[-8:]

    found_index = None
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if message.get("role") != "user":
            continue
        text = first_text(message.get("content"))
        if text.strip() == target:
            found_index = idx
            break
    if found_index is None:
        return messages[-8:]
    return messages[found_index:]


def run_summary(run: dict[str, Any], index: int) -> str:
    completed = run.get("completed") or {}
    compiled = run.get("compiled") or {}
    artifacts = run.get("artifacts") or {}

    completed_data = completed.get("data") or {}
    artifacts_data = artifacts.get("data") or {}
    usage = completed_data.get("usage") or {}
    final_prompt_text = str(completed_data.get("finalPromptText") or "")

    messages = completed_data.get("messagesSnapshot") or (compiled.get("data") or {}).get("messages") or []
    current_messages = locate_current_slice(messages, final_prompt_text)

    user_line = summarize_user_prompt(final_prompt_text)
    assistant_reply = ""
    tool_steps: list[ToolStep] = []
    pending_tool: dict[str, ToolStep] = {}

    for message in current_messages:
        role = message.get("role")
        if role == "assistant":
            tool_call = first_tool_call(message.get("content"))
            if tool_call:
                name = str(tool_call.get("name") or "tool")
                arguments = tool_call.get("arguments") or {}
                summary = ""
                if name == "read":
                    summary = shorten(str(arguments.get("path") or ""), 140)
                elif name == "exec":
                    summary = summarize_command(str(arguments.get("command") or ""))
                else:
                    summary = shorten(json.dumps(arguments, ensure_ascii=False), 140)
                step = ToolStep(name=name, summary=summary, result="")
                tool_steps.append(step)
                call_id = str(tool_call.get("id") or "")
                if call_id:
                    pending_tool[call_id] = step
                continue

            text = first_text(message.get("content"))
            if text:
                assistant_reply = text

        elif role == "toolResult":
            text = first_text(message.get("content"))
            tool_name = str(message.get("toolName") or "tool")
            call_id = str(message.get("toolCallId") or "")
            result = summarize_tool_result(tool_name, text)
            if call_id and call_id in pending_tool:
                pending_tool[call_id].result = result
            elif tool_steps:
                tool_steps[-1].result = result

    if not assistant_reply:
        assistant_texts = artifacts_data.get("assistantTexts") or completed_data.get("assistantTexts") or []
        if assistant_texts:
            assistant_reply = str(assistant_texts[-1])

    lines = []
    lines.append(f"Run {index}: {run['run_id'][:8]}  {completed.get('ts', run['started'].get('ts', ''))}")
    lines.append(f"  User: {user_line}")
    lines.append(
        "  Tokens:"
        f" input={usage.get('input', 0)}"
        f" output={usage.get('output', 0)}"
        f" cacheRead={usage.get('cacheRead', 0)}"
        f" total={usage.get('total', usage.get('totalTokens', 0))}"
    )

    if tool_steps:
        lines.append(f"  Tools ({len(tool_steps)}):")
        for idx, step in enumerate(tool_steps, 1):
            lines.append(f"    {idx}. {step.name}: {step.summary}")
            if step.result:
                lines.append(f"       -> {step.result}")
    else:
        lines.append("  Tools: none")

    if assistant_reply:
        reply_line = shorten(assistant_reply.replace("\n", " "), 180)
        lines.append(f"  Reply: {reply_line}")
        if assistant_reply.strip() == "NO_REPLY":
            lines.append("  Note: command turn still entered the model, but produced no visible reply.")

    return "\n".join(lines)


trajectory_path = resolve_trajectory_path()
raw_lines = [json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines() if line.strip()]
runs = extract_runs(raw_lines)
if not runs:
    fail(f"no runs found in trajectory file: {trajectory_path}")

first_started = runs[0]["started"]
last_completed = next((run["completed"] for run in reversed(runs) if run.get("completed")), None) or {}
session_id = first_started.get("sessionId", trajectory_path.name.replace(".trajectory.jsonl", ""))
session_key = first_started.get("sessionKey", "")

print(f"Trajectory: {trajectory_path}")
print(f"Session ID: {session_id}")
if session_key:
    print(f"Session Key: {session_key}")
print(f"Runs: {len(runs)}")
print(f"Window: {first_started.get('ts', '')} -> {last_completed.get('ts', '')}")
print("")

command_turns = 0
for idx, run in enumerate(runs, 1):
    summary = run_summary(run, idx)
    print(summary)
    print("")
    if "NO_REPLY" in summary:
        command_turns += 1

print(f"Summary: {len(runs)} model runs, {command_turns} NO_REPLY command runs.")
PY
