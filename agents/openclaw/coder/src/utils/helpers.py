from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def shift_utc_timestamp(value: str | None, *, seconds: int) -> str | None:
    timestamp = parse_utc_timestamp(value)
    if timestamp is None:
        return None
    shifted = timestamp + timedelta(seconds=seconds)
    return shifted.strftime("%Y-%m-%dT%H:%M:%SZ")


def newer_utc_timestamp(left: str | None, right: str | None) -> str | None:
    if not left:
        return right
    if not right:
        return left
    return left if left >= right else right


def short_text(text: str, limit: int = 1000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def tail_text(text: str, limit: int = 3000) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


def slugify(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    if not text:
        return "task"
    return text[:limit].strip("-") or "task"


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def service_actor_name() -> str:
    return "Coder"


def backend_label(config: dict[str, Any]) -> str:
    backend = str(config.get("execution_backend") or "codex").strip().lower()
    if backend == "codex":
        return "Codex"
    if backend == "claude":
        return "Claude"
    return backend.capitalize() or "Codex"


def backend_model_label(config: dict[str, Any]) -> str:
    backend = str(config.get("execution_backend") or "codex").strip().lower()
    if backend == "claude":
        return str(config.get("claude_model") or "")
    if backend == "codex":
        return str(config.get("codex_model") or "")
    return ""


def run_command(
    command: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    shell: bool = False,
    stdin: Any | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=timeout,
        shell=shell,
        stdin=stdin,
        check=False,
    )


def command_exists(command: str) -> bool:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or "/" in command:
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def path_readable(path: str) -> bool:
    target = Path(path)
    return target.is_file() and os.access(target, os.R_OK)


def ensure_writable_path(target: Path, *, is_file: bool) -> None:
    if is_file:
        parent = ensure_dir(target.parent)
        probe = parent / f".write-test-{os.getpid()}"
    else:
        directory = ensure_dir(target)
        probe = directory / f".write-test-{os.getpid()}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
