from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from src.clients.github_client import build_app_jwt, get_installation_token
from src.db import init_db
from src.utils.helpers import (
    command_exists,
    ensure_writable_path,
    path_readable,
    run_command,
    service_actor_name,
    tail_text,
)


APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_FORK_OWNER = "YeYing2025"
DEFAULT_EXECUTION_BACKEND = "codex"
SUPPORTED_EXECUTION_BACKENDS = {"codex", "claude"}
DEFAULT_GIT_AUTHOR_EMAIL = "coder-bot@local"
DEFAULT_SUPPORTED_CODEX_MODELS = (
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
)
DEFAULT_SUPPORTED_CLAUDE_MODELS = (
    "claude-haiku-4-5-20251001",
    "claude-opus-4-6",
    "claude-opus-4-6-thinking",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
)


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from an env file into os.environ without overriding existing values."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def env_csv(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or list(default)


def env_mapping(name: str) -> dict[str, str]:
    raw = os.getenv(name)
    if raw is None:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(","):
        pair = item.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def resolve_path_value(value: str, *, base_dir: Path) -> Path:
    target = Path(value).expanduser()
    if not target.is_absolute():
        target = base_dir / target
    return target.resolve(strict=False)


def env_path(name: str, default: Path, *, base_dir: Path) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return str(default.resolve(strict=False))
    return str(resolve_path_value(raw.strip(), base_dir=base_dir))


def env_command(name: str, default: str, *, base_dir: Path) -> str:
    raw = os.getenv(name)
    value = (raw if raw is not None else default).strip()
    if "/" in value:
        return str(resolve_path_value(value, base_dir=base_dir))
    return value


def prefer_existing_path(preferred: Path, legacy: Path | None = None) -> Path:
    if preferred.exists():
        return preferred.resolve(strict=False)
    if legacy is not None and legacy.exists():
        return legacy.resolve(strict=False)
    return preferred.resolve(strict=False)


def read_config(env_file_path: Path | None = None) -> dict[str, Any]:
    default_env_path = prefer_existing_path(
        APP_DIR / "config" / "coder-bot.env",
        APP_DIR / ".env",
    )
    actual_env_path = (env_file_path or default_env_path).expanduser().resolve(strict=False)
    env_base_dir = actual_env_path.parent
    default_app_home = ".." if env_base_dir.name == "config" else "."
    app_home = resolve_path_value(os.getenv("APP_HOME", default_app_home), base_dir=env_base_dir)
    data_dir = resolve_path_value(os.getenv("DATA_DIR", "data"), base_dir=app_home)
    secrets_dir = resolve_path_value(os.getenv("SECRETS_DIR", "secrets"), base_dir=app_home)
    default_codex_source_home = Path.home() / ".codex"
    default_codex_runtime_home = (data_dir / "codex" / "home").resolve(strict=False)
    allowed_repos = [
        item.strip()
        for item in os.getenv("ALLOWED_REPOS", "").split(",")
        if item.strip()
    ]
    if not allowed_repos:
        owner = os.getenv("GITHUB_OWNER", "").strip()
        repo = os.getenv("GITHUB_REPO", "").strip()
        if owner and repo:
            allowed_repos = [f"{owner}/{repo}"]

    return {
        "app_home": str(app_home),
        "data_dir": str(data_dir),
        "secrets_dir": str(secrets_dir),
        "listen_host": os.getenv("LISTEN_HOST", "0.0.0.0"),
        "listen_port": env_int("LISTEN_PORT", 9081),
        "github_api_url": os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        "github_app_id": os.getenv("GITHUB_APP_ID", "").strip(),
        "github_installation_id": os.getenv("GITHUB_INSTALLATION_ID", "").strip(),
        "github_fork_installation_id": os.getenv("GITHUB_FORK_INSTALLATION_ID", "").strip(),
        "github_private_key_path": env_path(
            "GITHUB_PRIVATE_KEY_PATH",
            secrets_dir / "github-app.pem",
            base_dir=app_home,
        ),
        "github_clone_ssh_key_path": env_path(
            "GITHUB_CLONE_SSH_KEY_PATH",
            secrets_dir / "github-push-key",
            base_dir=app_home,
        ),
        "github_fork_owner": os.getenv("GITHUB_FORK_OWNER", DEFAULT_FORK_OWNER).strip()
        or DEFAULT_FORK_OWNER,
        "allowed_repos": allowed_repos,
        "execution_backend": (
            os.getenv("EXECUTION_BACKEND", DEFAULT_EXECUTION_BACKEND).strip().lower()
            or DEFAULT_EXECUTION_BACKEND
        ),
        "repo_aliases": env_mapping("REPO_ALIASES"),
        "issue_session_prefix": os.getenv("ISSUE_SESSION_PREFIX", "gh").strip() or "gh",
        "codex_bin": env_command("CODEX_BIN", "codex", base_dir=app_home),
        "codex_model": os.getenv("CODEX_MODEL", "").strip(),
        "supported_codex_models": env_csv("SUPPORTED_CODEX_MODELS", list(DEFAULT_SUPPORTED_CODEX_MODELS)),
        "codex_timeout": env_int("CODEX_TIMEOUT", 3600),
        "codex_source_home": env_path(
            "CODEX_SOURCE_HOME",
            default_codex_source_home,
            base_dir=app_home,
        ),
        "codex_runtime_home": env_path(
            "CODEX_RUNTIME_HOME",
            default_codex_runtime_home,
            base_dir=app_home,
        ),
        "codex_use_dangerously_bypass": env_bool("CODEX_USE_DANGEROUSLY_BYPASS", True),
        "claude_bin": env_command("CLAUDE_BIN", "claude", base_dir=app_home),
        "claude_model": os.getenv("CLAUDE_MODEL", "").strip(),
        "supported_claude_models": env_csv("SUPPORTED_CLAUDE_MODELS", list(DEFAULT_SUPPORTED_CLAUDE_MODELS)),
        "claude_timeout": env_int("CLAUDE_TIMEOUT", 3600),
        "issue_label_accepted_name": os.getenv("ISSUE_LABEL_ACCEPTED_NAME", "已受理").strip() or "已受理",
        "issue_label_accepted_color": os.getenv("ISSUE_LABEL_ACCEPTED_COLOR", "0075ca").strip() or "0075ca",
        "issue_label_in_progress_name": os.getenv("ISSUE_LABEL_IN_PROGRESS_NAME", "开发中").strip() or "开发中",
        "issue_label_in_progress_color": os.getenv("ISSUE_LABEL_IN_PROGRESS_COLOR", "d4c5f9").strip() or "d4c5f9",
        "issue_label_pr_ready_name": os.getenv("ISSUE_LABEL_PR_READY_NAME", "待合并").strip() or "待合并",
        "issue_label_pr_ready_color": os.getenv("ISSUE_LABEL_PR_READY_COLOR", "0e8a16").strip() or "0e8a16",
        "issue_branch_prefix": os.getenv("ISSUE_BRANCH_PREFIX", "coder").strip() or "coder",
        "feishu_app_id": os.getenv("FEISHU_APP_ID", "").strip(),
        "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", "").strip(),
        "feishu_handoff_chat_id": os.getenv("FEISHU_HANDOFF_CHAT_ID", "").strip(),
        "feishu_handoff_chat_ids": env_csv("FEISHU_HANDOFF_CHAT_IDS", []),
        "feishu_confirm_keywords": env_csv(
            "FEISHU_CONFIRM_KEYWORDS",
            ["/run", "开始执行", "确认执行", "可以执行", "方案1", "方案一", "执行方案1", "执行方案一"],
        ),
        "feishu_chat_scan_limit": env_int("FEISHU_CHAT_SCAN_LIMIT", 30),
        "feishu_thread_scan_limit": env_int("FEISHU_THREAD_SCAN_LIMIT", 30),
        "db_path": env_path(
            "DB_PATH",
            data_dir / "issue_bot.db",
            base_dir=app_home,
        ),
        "repo_root": env_path(
            "REPO_ROOT",
            data_dir / "repos",
            base_dir=app_home,
        ),
        "repo_lock_wait_seconds": env_int("REPO_LOCK_WAIT_SECONDS", 7200),
        "project_root": str(app_home),
        "gunicorn_config_path": str((app_home / "config" / "gunicorn.conf.py").resolve(strict=False)),
        "job_root": env_path(
            "JOB_ROOT",
            data_dir / "jobs",
            base_dir=app_home,
        ),
        "active_dir": env_path(
            "ACTIVE_DIR",
            data_dir / "active",
            base_dir=app_home,
        ),
        "sync_script_path": os.getenv("SYNC_SCRIPT_PATH", "scripts/sync.sh").strip(),
        "git_author_name": os.getenv("GIT_AUTHOR_NAME", service_actor_name()).strip()
        or service_actor_name(),
        "git_author_email": os.getenv("GIT_AUTHOR_EMAIL", DEFAULT_GIT_AUTHOR_EMAIL).strip()
        or DEFAULT_GIT_AUTHOR_EMAIL,
        "pr_title_prefix": os.getenv("PR_TITLE_PREFIX", "[Coder]").strip() or "[Coder]",
        "submit_comment_after_pr": env_bool("SUBMIT_COMMENT_AFTER_PR", True),
        "submit_comment_body": os.getenv("SUBMIT_COMMENT_BODY", "/submit").strip() or "/submit",
        "default_base_branch": os.getenv("DEFAULT_BASE_BRANCH", "").strip(),
        "test_command": os.getenv("TEST_COMMAND", "").strip(),
        "state_file": env_path(
            "STATE_FILE",
            data_dir / "state.json",
            base_dir=app_home,
        ),
        "log_dir": env_path(
            "LOG_DIR",
            data_dir / "logs",
            base_dir=app_home,
        ),
        "dispatch_interval_seconds": env_int("DISPATCH_INTERVAL_SECONDS", 5),
        "fork_wait_timeout_seconds": env_int("FORK_WAIT_TIMEOUT_SECONDS", 300),
    }


def collect_config_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    backend = config["execution_backend"]

    if not config["allowed_repos"]:
        errors.append("ALLOWED_REPOS 不能为空。")

    for key in ["github_app_id", "github_installation_id"]:
        if not config.get(key):
            errors.append(f"{key.upper()} 不能为空。")

    if not config["github_private_key_path"]:
        errors.append("GITHUB_PRIVATE_KEY_PATH 不能为空。")
    elif not path_readable(config["github_private_key_path"]):
        errors.append("GITHUB_PRIVATE_KEY_PATH 指向的私钥文件不存在或不可读。")

    if not config["github_clone_ssh_key_path"]:
        errors.append("GITHUB_CLONE_SSH_KEY_PATH 不能为空。")
    elif not path_readable(config["github_clone_ssh_key_path"]):
        errors.append("GITHUB_CLONE_SSH_KEY_PATH 指向的 SSH 私钥不存在或不可读。")

    if backend not in SUPPORTED_EXECUTION_BACKENDS:
        errors.append("EXECUTION_BACKEND 只支持：" + "、".join(sorted(SUPPORTED_EXECUTION_BACKENDS)))

    if not config["codex_bin"]:
        errors.append("CODEX_BIN 不能为空。")
    elif not command_exists(config["codex_bin"]):
        errors.append("CODEX_BIN 不存在或不可执行。")

    source_home = Path(str(config["codex_source_home"])).expanduser()
    runtime_home = Path(str(config["codex_runtime_home"])).expanduser()
    source_auth = source_home / "auth.json"
    runtime_auth = runtime_home / "auth.json"
    if not source_auth.exists() and not runtime_auth.exists():
        errors.append("CODEX_SOURCE_HOME 或 CODEX_RUNTIME_HOME 下至少要有一份可用的 auth.json。")

    if not config["feishu_app_id"]:
        errors.append("FEISHU_APP_ID 不能为空。")
    if not config["feishu_app_secret"]:
        errors.append("FEISHU_APP_SECRET 不能为空。")
    feishu_chat_ids = [
        str(item).strip()
        for item in [config.get("feishu_handoff_chat_id"), *(config.get("feishu_handoff_chat_ids") or [])]
        if str(item or "").strip()
    ]
    if not feishu_chat_ids:
        errors.append("FEISHU_HANDOFF_CHAT_ID 或 FEISHU_HANDOFF_CHAT_IDS 至少要配置一个群聊。")

    return errors


def validate_config(config: dict[str, Any]) -> None:
    errors = collect_config_errors(config)
    if errors:
        raise SystemExit("配置校验失败：\n- " + "\n- ".join(errors))


def run_doctor(config: dict[str, Any], env_file: Path) -> int:
    results: list[tuple[str, bool, str]] = []
    backend = config["execution_backend"]

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    check("env 文件", env_file.exists(), str(env_file.resolve(strict=False)))

    config_errors = collect_config_errors(config)
    if config_errors:
        check("配置基础校验", False, "；".join(config_errors))
    else:
        check("配置基础校验", True, "必填项和开关组合正常")

    private_key_path = Path(config["github_private_key_path"])
    check("GitHub App 私钥", path_readable(config["github_private_key_path"]), str(private_key_path))

    ssh_key_path = Path(config["github_clone_ssh_key_path"])
    check("Git SSH 私钥", path_readable(config["github_clone_ssh_key_path"]), str(ssh_key_path))

    check("执行后端", backend in SUPPORTED_EXECUTION_BACKENDS, backend)

    codex_exists = command_exists(config["codex_bin"])
    check("Codex 可执行文件", codex_exists, config["codex_bin"])
    if codex_exists:
        try:
            codex_probe = run_command(
                [config["codex_bin"], "--version"],
                cwd=Path(config["app_home"]),
                timeout=30,
            )
            probe_output = tail_text(
                "\n".join(part for part in [codex_probe.stdout, codex_probe.stderr] if part),
                500,
            ) or config["codex_bin"]
            check("Codex CLI 可运行", codex_probe.returncode == 0, probe_output)
        except Exception as exc:
            check("Codex CLI 可运行", False, str(exc))
    else:
        check("Codex CLI 可运行", False, "skipped: executable missing")

    claude_exists = command_exists(config.get("claude_bin", "claude"))
    check("Claude 可执行文件", claude_exists, config.get("claude_bin", "claude"))
    if claude_exists:
        try:
            claude_probe = run_command(
                [config["claude_bin"], "--version"],
                cwd=Path(config["app_home"]),
                timeout=30,
            )
            probe_output = tail_text(
                "\n".join(part for part in [claude_probe.stdout, claude_probe.stderr] if part),
                500,
            ) or config["claude_bin"]
            check("Claude CLI 可运行", claude_probe.returncode == 0, probe_output)
        except Exception as exc:
            check("Claude CLI 可运行", False, str(exc))
    else:
        check("Claude CLI 可运行", False, "skipped: executable missing")

    check("Feishu App ID", bool(config["feishu_app_id"]), config["feishu_app_id"] or "(empty)")
    check(
        "Feishu Handoff Chats",
        bool(config["feishu_handoff_chat_id"] or config["feishu_handoff_chat_ids"]),
        ", ".join(
            [
                str(item).strip()
                for item in [config.get("feishu_handoff_chat_id"), *(config.get("feishu_handoff_chat_ids") or [])]
                if str(item or "").strip()
            ]
        ) or "(empty)",
    )

    check("GitHub CLI", command_exists("gh"), "gh")
    gunicorn_candidates = [
        "gunicorn",
        str((Path(config["app_home"]) / ".venv" / "bin" / "gunicorn").resolve(strict=False)),
    ]
    gunicorn_available = importlib.util.find_spec("gunicorn") is not None or any(
        command_exists(candidate) for candidate in gunicorn_candidates
    )
    check("Gunicorn", gunicorn_available, gunicorn_candidates[-1] if gunicorn_available else "gunicorn")

    try:
        ensure_writable_path(Path(config["db_path"]), is_file=True)
        ensure_writable_path(Path(config["job_root"]), is_file=False)
        ensure_writable_path(Path(config["repo_root"]), is_file=False)
        ensure_writable_path(Path(config["active_dir"]), is_file=False)
        ensure_writable_path(Path(config["state_file"]), is_file=True)
        ensure_writable_path(Path(config["log_dir"]), is_file=False)
        detail_parts = [
            "DB_PATH",
            "JOB_ROOT",
            "REPO_ROOT",
            "ACTIVE_DIR",
            "STATE_FILE",
            "LOG_DIR",
        ]
        ensure_writable_path(Path(config["codex_runtime_home"]), is_file=False)
        detail_parts.append("CODEX_RUNTIME_HOME")
        detail = " / ".join(detail_parts) + " 可写"
        check("目录写权限", True, detail)
    except Exception as exc:
        check("目录写权限", False, str(exc))

    try:
        init_db(config)
        check("SQLite", True, config["db_path"])
    except Exception as exc:
        check("SQLite", False, str(exc))

    try:
        build_app_jwt(config)
        check("GitHub App JWT", True, "私钥可解析")
    except Exception as exc:
        check("GitHub App JWT", False, str(exc))

    try:
        get_installation_token(config)
        check("GitHub Installation Token", True, "access token acquired")
    except Exception as exc:
        check("GitHub Installation Token", False, str(exc))

    if config["github_fork_installation_id"]:
        try:
            get_installation_token(config, config["github_fork_installation_id"])
            check("Fork Installation Token", True, "access token acquired")
        except Exception as exc:
            check("Fork Installation Token", False, str(exc))
    else:
        check("Fork Installation Token", True, "未配置，表示使用手工创建的 fork")

    print("Coder Bot Doctor")
    print(f"APP_HOME: {config['app_home']}")
    print(f"DATA_DIR: {config['data_dir']}")
    print(f"SECRETS_DIR: {config['secrets_dir']}")
    print(f"EXECUTION_BACKEND: {backend}")
    print(f"ALLOWED_REPOS: {', '.join(config['allowed_repos']) or '(empty)'}")
    print("")
    for name, ok, detail in results:
        prefix = "[OK]" if ok else "[FAIL]"
        print(f"{prefix} {name}: {detail}")

    failed = [item for item in results if not item[1]]
    print("")
    if failed:
        print(f"Doctor 完成：{len(failed)} 项失败。")
        return 1

    print("Doctor 完成：全部通过。")
    return 0


def default_env_file_path() -> Path:
    raw = (
        os.getenv("CODER_BOT_ENV_FILE")
        or os.getenv("CODING_BOT_ENV_FILE")
        or str(prefer_existing_path(APP_DIR / "config" / "coder-bot.env", APP_DIR / ".env"))
    )
    return Path(raw).expanduser().resolve(strict=False)
