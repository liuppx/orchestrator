from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from src.clients.github_client import (
    comment_issue,
    create_fork,
    create_pull_request,
    get_installation_token,
    get_repo_info,
    get_repo_info_optional,
    list_pull_requests,
)
from src.clients.codex_client import (
    build_codex_env,
    parse_codex_jsonl_error_messages,
    parse_codex_jsonl_events,
    parse_codex_jsonl_usage,
)
from src.utils.helpers import (
    ensure_dir,
    now_utc,
    run_command,
    service_actor_name,
    short_text,
    slugify,
    tail_text,
)


DEFAULT_GIT_AUTHOR_EMAIL = "coder-bot@local"
JOB_TOKEN_USAGE_FILENAME = "token_usage.json"
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass
class WorkerContext:
    config: dict[str, Any]
    get_job: Callable[[str], sqlite3.Row | None]
    mark_job_running: Callable[[str, int], None]
    mark_job_finished: Callable[..., None]
    clear_issue_active_job: Callable[[str, int, str], None]
    ensure_issue_session: Callable[[str, int, str], sqlite3.Row]
    upsert_issue_session: Callable[..., sqlite3.Row]
    cleanup_closed_issue_if_finished: Callable[[str, int], None]
    reply_issue_progress_to_feishu: Callable[..., None]
    reply_issue_execution_result_to_feishu: Callable[..., None]


def build_issue_agent_id(config: dict[str, Any], repo_full_name: str, issue_number: int) -> str:
    prefix = slugify(config.get("issue_session_prefix", "gh"), limit=12)
    repo_slug = slugify(repo_full_name.replace("/", "-"), limit=48)
    if prefix:
        return f"{prefix}-{repo_slug}-issue-{issue_number}"
    return f"{repo_slug}-issue-{issue_number}"


def empty_token_usage() -> dict[str, int]:
    return {key: 0 for key in TOKEN_USAGE_KEYS}


def merge_token_usage(base: dict[str, int], extra: dict[str, int] | None) -> dict[str, int]:
    if not extra:
        return base
    for key in TOKEN_USAGE_KEYS:
        base[key] = int(base.get(key) or 0) + int(extra.get(key) or 0)
    return base


def normalize_token_usage(value: dict[str, Any] | None) -> dict[str, int] | None:
    if not value:
        return None
    usage = empty_token_usage()
    for key in TOKEN_USAGE_KEYS:
        usage[key] = int(value.get(key) or 0)
    if not any(usage.values()):
        return None
    return usage


def job_token_usage_path(job_dir: Path) -> Path:
    return Path(job_dir) / JOB_TOKEN_USAGE_FILENAME


def write_job_token_usage(job_dir: Path, token_usage: dict[str, Any] | None) -> None:
    usage = normalize_token_usage(token_usage)
    if usage is None:
        return
    job_token_usage_path(job_dir).write_text(
        json.dumps(usage, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def read_job_token_usage(job_dir: Path) -> dict[str, int] | None:
    path = job_token_usage_path(job_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_token_usage(payload)


def job_metadata_path(job_dir: Path) -> Path:
    return Path(job_dir) / "job.json"


def read_job_metadata(job_dir: Path) -> dict[str, Any] | None:
    path = job_metadata_path(job_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def update_job_execution_metadata(job_dir: Path, **updates: Any) -> None:
    payload = read_job_metadata(job_dir)
    if payload is None:
        return
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    execution.update({key: value for key, value in updates.items()})
    payload["execution"] = execution
    job_metadata_path(job_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def repo_workspace_root(config: dict[str, Any], repo_full_name: str) -> Path:
    safe_name = repo_full_name.replace("/", "__")
    return ensure_dir(Path(config["repo_root"]) / safe_name)


def repo_issue_root(config: dict[str, Any], repo_full_name: str, issue_number: int) -> Path:
    return repo_workspace_root(config, repo_full_name) / "issues" / f"issue-{issue_number}"


def repo_checkout_dir(config: dict[str, Any], repo_full_name: str, issue_number: int) -> Path:
    return repo_issue_root(config, repo_full_name, issue_number) / "repo"


def acquire_file_lock(target: Path) -> bool:
    try:
        fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(now_utc())
        return True
    except FileExistsError:
        return False


def active_lock_path(config: dict[str, Any], repo_full_name: str, issue_number: int) -> Path:
    safe_name = repo_full_name.replace("/", "__")
    return ensure_dir(config["active_dir"]) / f"{safe_name}__{issue_number}.lock"


def repo_lock_path(config: dict[str, Any], repo_full_name: str) -> Path:
    safe_name = repo_full_name.replace("/", "__")
    return ensure_dir(config["active_dir"]) / f"{safe_name}__repo.lock"


def acquire_active_lock(config: dict[str, Any], repo_full_name: str, issue_number: int) -> bool:
    return acquire_file_lock(active_lock_path(config, repo_full_name, issue_number))


def acquire_repo_lock(config: dict[str, Any], repo_full_name: str) -> bool:
    target = repo_lock_path(config, repo_full_name)
    deadline = time.time() + max(60, config["repo_lock_wait_seconds"])
    while True:
        if acquire_file_lock(target):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def release_active_lock(config: dict[str, Any], repo_full_name: str, issue_number: int) -> None:
    target = active_lock_path(config, repo_full_name, issue_number)
    if target.exists():
        target.unlink()


def release_repo_lock(config: dict[str, Any], repo_full_name: str) -> None:
    target = repo_lock_path(config, repo_full_name)
    if target.exists():
        target.unlink()


def build_git_ssh_env(config: dict[str, Any], base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = (base_env or os.environ).copy()
    env["GIT_SSH_COMMAND"] = (
        f"ssh -F /dev/null -i {config['github_clone_ssh_key_path']} "
        "-o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no"
    )
    return env


def build_git_commit_env(config: dict[str, Any], base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = (base_env or os.environ).copy()
    author_name = str(config["git_author_name"]).strip() or service_actor_name()
    author_email = str(config["git_author_email"]).strip() or DEFAULT_GIT_AUTHOR_EMAIL
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    return env


def ensure_git_remote(cwd: Path, name: str, url: str) -> None:
    current_result = run_command(["git", "remote", "get-url", name], cwd=cwd, timeout=30)
    if current_result.returncode == 0:
        current_url = (current_result.stdout or "").strip()
        if current_url != url:
            set_result = run_command(["git", "remote", "set-url", name, url], cwd=cwd, timeout=30)
            if set_result.returncode != 0:
                raise RuntimeError(f"git remote set-url {name} failed\n{set_result.stderr}")
    else:
        add_result = run_command(["git", "remote", "add", name, url], cwd=cwd, timeout=30)
        if add_result.returncode != 0:
            raise RuntimeError(f"git remote add {name} failed\n{add_result.stderr}")

    push_result = run_command(["git", "remote", "get-url", "--push", name], cwd=cwd, timeout=30)
    current_push_url = (push_result.stdout or "").strip() if push_result.returncode == 0 else ""
    if current_push_url != url:
        set_push_result = run_command(["git", "remote", "set-url", "--push", name, url], cwd=cwd, timeout=30)
        if set_push_result.returncode != 0:
            raise RuntimeError(f"git remote set-url --push {name} failed\n{set_push_result.stderr}")


def git_remote_exists(config: dict[str, Any], remote_url: str) -> bool:
    result = run_command(
        ["git", "ls-remote", remote_url],
        cwd=ensure_dir(config["repo_root"]),
        env=build_git_ssh_env(config),
        timeout=120,
    )
    return result.returncode == 0


def ensure_fork_exists(config: dict[str, Any], upstream_owner: str, repo: str) -> None:
    fork_owner = config["github_fork_owner"]
    fork_url = f"git@github.com:{fork_owner}/{repo}.git"
    if git_remote_exists(config, fork_url):
        return

    fork_installation_id = config["github_fork_installation_id"]
    if not fork_installation_id:
        raise RuntimeError(
            f"fork {fork_owner}/{repo} does not exist or is unreachable; "
            "set GITHUB_FORK_INSTALLATION_ID for auto-fork, or create the fork manually"
        )

    fork_token = get_installation_token(config, fork_installation_id)
    existing = get_repo_info_optional(config, fork_token, fork_owner, repo)
    if existing:
        return

    create_fork(config, fork_token, upstream_owner, repo)
    deadline = time.time() + max(60, config["fork_wait_timeout_seconds"])
    while time.time() < deadline:
        info = get_repo_info_optional(config, fork_token, fork_owner, repo)
        if info and git_remote_exists(config, fork_url):
            return
        time.sleep(5)
    raise RuntimeError(f"timed out waiting for fork {fork_owner}/{repo} to become available")


def ensure_clean_worktree(repo_dir: Path) -> None:
    reset_result = run_command(["git", "reset", "--hard"], cwd=repo_dir, timeout=60)
    if reset_result.returncode != 0:
        raise RuntimeError(f"git reset failed\n{reset_result.stderr}")

    clean_result = run_command(["git", "clean", "-fdx"], cwd=repo_dir, timeout=120)
    if clean_result.returncode != 0:
        raise RuntimeError(f"git clean failed\n{clean_result.stderr}")


def git_fetch_remote(config: dict[str, Any], repo_dir: Path, remote: str, *, label: str) -> None:
    fetch_result = run_command(
        ["git", "fetch", "--prune", remote],
        cwd=repo_dir,
        env=build_git_ssh_env(config),
        timeout=300,
    )
    if fetch_result.returncode != 0:
        raise RuntimeError(f"git fetch {label} failed\n{fetch_result.stderr}")


def git_checkout_branch(repo_dir: Path, branch_name: str, start_point: str, *, label: str) -> None:
    checkout_result = run_command(
        ["git", "checkout", "-B", branch_name, start_point],
        cwd=repo_dir,
        timeout=60,
    )
    if checkout_result.returncode != 0:
        raise RuntimeError(f"git checkout {label} failed\n{checkout_result.stderr}")


def git_ref_exists(repo_dir: Path, ref_name: str) -> bool:
    result = run_command(
        ["git", "rev-parse", "--verify", "--quiet", ref_name],
        cwd=repo_dir,
        timeout=30,
    )
    return result.returncode == 0


def repo_checkout_is_usable(repo_dir: Path) -> bool:
    if not (repo_dir / ".git").exists():
        return False
    worktree_result = run_command(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        timeout=30,
    )
    if worktree_result.returncode != 0 or (worktree_result.stdout or "").strip() != "true":
        return False
    refs_result = run_command(
        ["git", "show-ref", "--head", "--quiet"],
        cwd=repo_dir,
        timeout=30,
    )
    return refs_result.returncode == 0


def git_current_branch(repo_dir: Path) -> str:
    result = run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_dir,
        timeout=30,
    )
    branch = (result.stdout or "").strip() if result.returncode == 0 else ""
    if not branch:
        raise RuntimeError("could not determine current branch for PR publish")
    return branch


def git_status_entries(repo_dir: Path) -> list[tuple[str, str]]:
    result = run_command(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo_dir,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git status failed\n{result.stderr}")

    entries: list[tuple[str, str]] = []
    for raw_line in (result.stdout or "").splitlines():
        if len(raw_line) < 4:
            continue
        entries.append((raw_line[:2], raw_line[3:]))
    return entries


def remove_repo_path(target: Path) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
        return
    if target.exists() or target.is_symlink():
        target.unlink()


def build_issue_commit_message(issue_number: int, issue_title: str) -> str:
    title = " ".join((issue_title or "").split()).strip()
    if title:
        return f"{service_actor_name()}: resolve issue #{issue_number} {title}"
    return f"{service_actor_name()}: resolve issue #{issue_number}"


def commit_repo_changes(
    config: dict[str, Any],
    repo_dir: Path,
    issue_number: int,
    issue_title: str,
) -> str:
    add_result = run_command(
        ["git", "add", "-A", "--", "."],
        cwd=repo_dir,
        timeout=120,
    )
    if add_result.returncode != 0:
        raise RuntimeError(f"git add failed\n{tail_text(add_result.stderr or add_result.stdout, 3000)}")

    cached_diff = run_command(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir,
        timeout=60,
    )
    if cached_diff.returncode == 0:
        raise RuntimeError("executor reported succeeded but produced no commit-worthy repository changes")
    if cached_diff.returncode != 1:
        raise RuntimeError(
            "git diff --cached failed\n"
            f"{tail_text(cached_diff.stderr or cached_diff.stdout, 3000)}"
        )

    commit_result = run_command(
        ["git", "commit", "-m", build_issue_commit_message(issue_number, issue_title)],
        cwd=repo_dir,
        env=build_git_commit_env(config),
        timeout=180,
    )
    if commit_result.returncode != 0:
        raise RuntimeError(
            "git commit failed\n"
            f"{tail_text(commit_result.stderr or commit_result.stdout, 3000)}"
        )

    rev_result = run_command(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        timeout=30,
    )
    commit_sha = (rev_result.stdout or "").strip() if rev_result.returncode == 0 else ""
    if not commit_sha:
        raise RuntimeError("git commit completed but HEAD sha could not be determined")
    return commit_sha


def ensure_repo_checkout(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    default_branch: str,
    branch_name: str,
) -> tuple[Path, Path]:
    upstream_owner, repo = repo_full_name.split("/", 1)
    fork_owner = config["github_fork_owner"]
    workspace_root = ensure_dir(repo_issue_root(config, repo_full_name, issue_number))
    repo_dir = repo_checkout_dir(config, repo_full_name, issue_number)
    fork_url = f"git@github.com:{fork_owner}/{repo}.git"
    upstream_url = f"git@github.com:{upstream_owner}/{repo}.git"

    ensure_fork_exists(config, upstream_owner, repo)

    if (repo_dir / ".git").exists() and not repo_checkout_is_usable(repo_dir):
        remove_repo_path(repo_dir)

    if not (repo_dir / ".git").exists():
        clone_result = run_command(
            ["git", "clone", fork_url, str(repo_dir)],
            cwd=workspace_root,
            env=build_git_ssh_env(config),
            timeout=300,
        )
        if clone_result.returncode != 0:
            raise RuntimeError(f"git clone failed\n{clone_result.stderr}")

    ensure_git_remote(repo_dir, "origin", fork_url)
    ensure_git_remote(repo_dir, "upstream", upstream_url)
    ensure_clean_worktree(repo_dir)
    git_fetch_remote(config, repo_dir, "origin", label="origin")
    git_fetch_remote(config, repo_dir, "upstream", label="upstream")

    sync_script = repo_dir / config["sync_script_path"]
    if sync_script.is_file():
        git_checkout_branch(repo_dir, default_branch, f"origin/{default_branch}", label="base")

        sync_env = build_git_ssh_env(config)
        sync_env["AUTO_PUSH"] = "true"
        sync_result = run_command(
            ["bash", str(sync_script)],
            cwd=repo_dir,
            env=sync_env,
            timeout=600,
        )
        combined_sync_output = "\n".join(part for part in [sync_result.stdout, sync_result.stderr] if part)
        if sync_result.returncode != 0:
            raise RuntimeError(f"sync.sh failed\n{tail_text(combined_sync_output, 3000)}")

        git_fetch_remote(config, repo_dir, "origin", label="origin after sync")
        branch_start = (
            f"origin/{branch_name}"
            if git_ref_exists(repo_dir, f"refs/remotes/origin/{branch_name}")
            else f"origin/{default_branch}"
        )
        git_checkout_branch(repo_dir, branch_name, branch_start, label="branch")
        return workspace_root, repo_dir

    git_checkout_branch(repo_dir, default_branch, f"upstream/{default_branch}", label="base")
    branch_start = (
        f"origin/{branch_name}"
        if git_ref_exists(repo_dir, f"refs/remotes/origin/{branch_name}")
        else f"upstream/{default_branch}"
    )
    git_checkout_branch(repo_dir, branch_name, branch_start, label="branch")

    return workspace_root, repo_dir


def publish_pull_request_via_git_push_and_api(
    config: dict[str, Any],
    token: str,
    work_dir: Path,
    upstream_owner: str,
    repo: str,
    issue_number: int,
    issue_title: str,
    base_branch: str,
    pr_title: str,
    pr_body: str,
) -> dict[str, Any]:
    current_branch = git_current_branch(work_dir)
    commit_sha = commit_repo_changes(config, work_dir, issue_number, issue_title)
    head_ref = f"{config['github_fork_owner']}:{current_branch}"

    push_result = run_command(
        ["git", "push", "--set-upstream", "origin", current_branch],
        cwd=work_dir,
        env=build_git_ssh_env(config),
        timeout=300,
    )
    combined_push_output = "\n".join(part for part in [push_result.stdout, push_result.stderr] if part)
    if push_result.returncode != 0:
        raise RuntimeError(f"git push failed\n{tail_text(combined_push_output, 3000)}")

    existing = list_pull_requests(
        config,
        token,
        upstream_owner,
        repo,
        head=head_ref,
        base=base_branch,
    )
    if existing:
        return {"html_url": existing[0]["html_url"], "method": "git+api", "commit_sha": commit_sha}
    try:
        pr = create_pull_request(
            config,
            token,
            upstream_owner,
            repo,
            title=pr_title,
            body=pr_body,
            head=head_ref,
            base=base_branch,
        )
    except (requests.ConnectionError, requests.Timeout):
        # GitHub may create the PR and then time out before the response body is read.
        existing_after_timeout = list_pull_requests(
            config,
            token,
            upstream_owner,
            repo,
            head=head_ref,
            base=base_branch,
        )
        if existing_after_timeout:
            return {
                "html_url": existing_after_timeout[0]["html_url"],
                "method": "git+api-after-timeout",
                "commit_sha": commit_sha,
            }
        raise RuntimeError(
            "github pr create timeout and no existing pull request found "
            f"for head={head_ref} base={base_branch}"
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 422:
            existing_after_conflict = list_pull_requests(
                config,
                token,
                upstream_owner,
                repo,
                head=head_ref,
                base=base_branch,
            )
            if existing_after_conflict:
                return {
                    "html_url": existing_after_conflict[0]["html_url"],
                    "method": "git+api-after-conflict",
                    "commit_sha": commit_sha,
                }
        raise
    return {"html_url": pr["html_url"], "method": "git+api", "commit_sha": commit_sha}


def user_friendly_error_summary(error_text: str) -> str:
    text = str(error_text or "").strip()
    lower_text = text.lower()
    if not text:
        return "(no error text)"
    if "github pr create timeout" in lower_text:
        return short_text(
            "执行已完成代码修改，但 GitHub 创建 PR 超时。请稍后重试 `/run`，"
            "系统会复用已推送分支或已有 PR。",
            700,
        )
    if (
        "api.github.com" in text
        and ("Read timed out" in text or "ConnectionError" in text or "ReadTimeout" in text)
    ) or "requests.exceptions.readtimeout" in lower_text:
        return short_text(
            "GitHub API 请求超时。代码可能已经提交并推送，"
            "但创建或查询 PR 时没有拿到 GitHub 响应；请稍后重试 `/run` 或检查是否已有同分支 PR。",
            700,
        )
    if (
        "git clone" in lower_text
        or "git fetch" in lower_text
        or "'git', 'clone'" in lower_text
        or "'git', 'fetch'" in lower_text
    ) and (
        "timed out after" in lower_text
        or "kex_exchange_identification" in lower_text
        or "could not read from remote repository" in lower_text
        or "banner exchange" in lower_text
        or "connection closed by unknown port 65535" in lower_text
    ):
        return short_text(
            "Git 拉取仓库失败。当前更像是 coder 机器到 GitHub 的 SSH/代理链路异常，不是代码修改本身的问题；"
            "请检查 GitHub SSH 直连或代理配置后重试。",
            700,
        )
    if "git push failed" in text:
        detail = tail_text(text.split("git push failed", 1)[-1].strip(), 360)
        return short_text(f"git push 失败，分支没有成功推送到 GitHub。请检查 deploy key、分支权限或网络后重试。\n{detail}", 700)
    if "no commit-worthy changes" in text or "no_change" in lower_text:
        return "没有检测到可提交的代码改动。可能是改动已经存在、模型没有修改文件，或上一次执行已经完成。"
    gateway_markers = (
        "503 service unavailable",
        "unexpected status 503",
        "api_error_status\": 503",
        "api_error_status 503",
        "无可用渠道",
        "当前分组",
    )
    if "router.yeying.pub" in text and any(marker in lower_text for marker in gateway_markers):
        return "模型网关暂时不可用或上游额度不足，Codex/Claude 调用没有完成；请稍后重试。"
    if any(marker in lower_text for marker in gateway_markers):
        return "模型网关暂时不可用或上游额度不足，模型调用没有完成；请稍后重试。"
    if "failed to authenticate" in lower_text or "authentication_failed" in lower_text or "401" in text:
        return "模型服务认证失败，可能是 Claude/Codex token 过期或当前模型无权限。请检查机器上的模型 CLI 配置后重试。"
    if "codex execution failed" in text:
        return short_text(tail_text(text.split("codex execution failed", 1)[-1].strip(), 500), 700)
    if "claude execution failed" in text:
        return short_text(tail_text(text.split("claude execution failed", 1)[-1].strip(), 500), 700)
    if "missing structured result" in lower_text:
        return "模型已返回内容，但没有按约定输出结构化执行结果；请在同一线程重试 `/run`。"
    if "bad credentials" in lower_text or "resource not accessible" in lower_text:
        return "GitHub 权限不足或凭证无效，无法完成 issue 评论、push 或 PR 操作。请检查 GitHub App/安装权限。"
    if "github_private_key_path" in text or "no such file or directory" in lower_text:
        return "运行配置或凭证文件缺失。请检查 coder 的 GitHub App 私钥、push key 和环境变量配置。"
    if "Traceback" in text:
        tail_lines = [line.strip() for line in text.splitlines() if line.strip()][-8:]
        return short_text(tail_text("\n".join(tail_lines), 500), 700)
    return short_text(tail_text(text, 500), 700)


def build_prompt(
    repo_full_name: str,
    issue: dict[str, Any],
    repo_path: str,
    test_command: str,
    *,
    session_key: str | None = None,
) -> str:
    issue_body = issue.get("body") or "(no issue body)"
    lines = [
        f"你正在处理 GitHub Issue #{issue['number']}。",
        f"仓库：{repo_full_name}",
        f"本地仓库路径：{repo_path}",
        f"Issue 标题：{issue['title']}",
    ]
    if session_key:
        lines.append(f"会话标识：{session_key}")
    lines.extend(
        [
            "",
            "执行阶段说明：",
            "- 飞书线程里已经收到明确的执行确认（例如 `/run` 或 `执行方案1`），现在就是正式执行阶段。",
            "- 不要再等待新的确认消息，不要因为“缺少 `/run`”返回 `needs_human`。",
            "- 如果历史上下文里还保留着讨论阶段的约束，以当前这条执行指令为准。",
            "",
            "工作区说明：",
            f"- 当前真正的 Git 仓库根目录只有：{repo_path}",
            "- 你的会话工作区可能比仓库根目录更大，但只有上面这个 repo 路径里的改动才会被提交。",
            "- 所有文件读写、编辑、检查都必须明确针对这个 repo 路径，不要把文件写到它的父目录、兄弟目录或其他工作区位置。",
            "- 如果你要新增文件，请直接写到这个 repo 路径下的目标位置。",
            "- 不要只根据推断声称“文件已经创建”或“修改已经完成”；必须用工具实际创建并再次读取验证。",
            "",
            "目标：",
            "- 只解决当前这个 issue。",
            "- 只在当前仓库内工作。",
            "- 采用最小化修改方案，不做无关重构。",
            "",
            "硬性限制：",
            "- 不要执行 git commit、git push、创建 PR、调用 sync.sh。",
            "- 不要修改 CI、部署配置、发布脚本、基础设施配置，除非 issue 明确要求且不改就无法解决。",
            "- 不要新增无关文档、示例文件、演示代码。",
            "- 不要修改其他仓库或访问当前仓库之外的路径。",
            "",
            "说明：",
            "- 这是同一个 Issue 的持续会话；如果之前已经分析过，请沿用已有结论，避免重复工作。",
            "- 你只负责修改工作区内容和必要的最小验证。",
            "- git commit、git push、创建 PR 会由外层智能体在你返回 `result: succeeded` 后自动执行。",
            "- 即使 Issue 正文要求“提交代码并创建 PR”，你也不要自己执行这些步骤，只要把代码改好并返回 `result: succeeded`。",
            "",
            "执行要求：",
            "1. 先阅读相关代码和 Issue 内容，再决定修改点。",
            "2. 优先复用现有实现和现有代码风格。",
            "3. 如果信息不足，做保守实现，并在最终结果里明确说明假设。",
            "4. 完成后运行最小必要验证。",
        ]
    )
    if test_command:
        lines.append(f"5. 如果配置了测试命令，执行：{test_command}")
    else:
        lines.append("5. 当前没有配置自动测试命令，至少做与你改动直接相关的最小自检。")
    lines.extend(
        [
            "6. 外层智能体会在你返回后检查 repo 内是否真的产生了可提交改动；不要把 `git status`、`gh issue view` 或其他 shell 命令当成继续任务的前置条件。",
            "7. 如果 shell/exec 工具不可用，继续使用可直接读写文件的工具完成修改和验证，不要因此中断。",
            f"8. 在输出 `result: succeeded` 前，必须再次读取你改动后的目标文件，确认它确实位于 `{repo_path}` 下且内容正确。",
            f"9. 如果你没有真正把改动落到 `{repo_path}` 里，绝对不允许输出 `result: succeeded`。",
            "",
            "最终只输出以下结构，不要输出其他无关内容：",
            "",
            "result: succeeded | no_change | needs_human",
            "",
            "summary:",
            "- 变更点1",
            "- 变更点2",
            "",
            "tests:",
            "- 执行的验证1",
            "- 执行的验证2",
            "",
            "risks:",
            "- 剩余风险1",
            "- 剩余风险2",
            "",
            "Issue 正文：",
            issue_body,
        ]
    )
    return "\n".join(lines).strip()


def build_missing_changes_retry_prompt(repo_path: str, previous_result: str) -> str:
    return "\n".join(
        [
            "上一轮你已经返回 `result: succeeded`，但外层检查发现 Git 仓库里仍然没有任何可提交改动。",
            "",
            f"真实 Git 仓库根目录：{repo_path}",
            "",
            "这通常意味着：",
            "- 你只描述了修改，但没有真正落文件；或者",
            "- 你把文件写到了仓库目录之外；或者",
            "- 你验证了错误的路径；或者",
            "- 你把 shell 工具失败误当成了任务已经完成。",
            "",
            "现在请立即修正：",
            f"1. 只在 `{repo_path}` 下真正落地所需修改。",
            "2. 优先使用可直接读写文件的工具，不要把 `git status`、`gh issue view` 或其他 shell 命令当成前置条件。",
            "3. 再次读取你修改后的目标文件，确认内容准确且路径正确。",
            "4. 外层会再次检查 repo 是否真的有改动；只有你确认文件已经实际写入后，才能输出 `result: succeeded`。",
            "5. 如果仍然无法在 repo 内产生改动，就输出 `result: needs_human` 并明确说明原因。",
            "",
            "你上一轮的回答如下：",
            previous_result.strip() or "(empty)",
            "",
            "请重新执行，并且最终仍然只输出约定的 result/summary/tests/risks 结构。",
        ]
    ).strip()


def parse_executor_result(text: str) -> dict[str, str]:
    raw = (text or "").strip()
    if not raw:
        raise RuntimeError("executor returned empty final response")
    match = re.search(r"(?mi)^\s*result:\s*(succeeded|no_change|needs_human)\s*$", raw)
    if not match:
        raise RuntimeError("executor final response missing `result:` line")
    return {"status": match.group(1), "text": raw}


def repo_has_commit_worthy_changes(repo_dir: Path) -> bool:
    for _status, _relative_path in git_status_entries(repo_dir):
        return True
    return False


def run_codex_chat_turn(
    config: dict[str, Any],
    work_dir: Path,
    prompt: str,
    *,
    resume_session_id: str | None = None,
    log_dir: Path | None = None,
    selected_model: str | None = None,
) -> dict[str, Any]:
    env = build_codex_env(config)
    temp_root = ensure_dir(Path(config["data_dir"]) / "codex" / "tmp")
    output_path = (log_dir or temp_root) / f"codex-last-message-{int(time.time() * 1000)}-{os.getpid()}.txt"
    requested_model = str(selected_model or "").strip() or None
    command = [
        config["codex_bin"],
        "exec",
    ]
    if resume_session_id:
        command.append("resume")
    command.extend(
        [
            "--json",
            "-o",
            str(output_path),
            "--skip-git-repo-check",
        ]
    )
    if not resume_session_id:
        command.extend(["-C", str(work_dir)])
    effective_model = str(selected_model or config.get("codex_model") or "").strip()
    if effective_model:
        command.extend(["-m", effective_model])
    if config.get("codex_use_dangerously_bypass", True):
        command.append("--dangerously-bypass-approvals-and-sandbox")
    if resume_session_id:
        command.append(str(resume_session_id))
    command.append(prompt)

    result = run_command(
        command,
        cwd=work_dir,
        env=env,
        timeout=int(config["codex_timeout"]) + 120,
    )
    if log_dir is not None:
        (log_dir / "codex.stdout.jsonl").write_text(result.stdout or "", encoding="utf-8")
        (log_dir / "codex.stderr.log").write_text(result.stderr or "", encoding="utf-8")
    if result.returncode != 0:
        error_messages = parse_codex_jsonl_error_messages(result.stdout or "")
        stderr_summary = tail_text(result.stderr or "", 2000)
        details: list[str] = []
        if error_messages:
            details.extend(error_messages[-3:])
        if stderr_summary and stderr_summary != "Reading additional input from stdin...":
            details.append(stderr_summary)
        if not details:
            fallback = tail_text(result.stdout or result.stderr, 4000)
            if fallback:
                details.append(fallback)
        raise RuntimeError(
            "codex execution failed\n"
            + "\n".join(details)
        )

    thread_id, streamed_text = parse_codex_jsonl_events(result.stdout or "")
    response_text = ""
    if output_path.exists():
        response_text = output_path.read_text(encoding="utf-8").strip()
    if not response_text:
        response_text = streamed_text or ""
    response_text = response_text.strip()
    if not response_text:
        raise RuntimeError("codex returned empty assistant reply")
    return {
        "agent_id": "codex",
        "backend": "codex",
        "agent_session_id": thread_id or resume_session_id,
        "requested_model": requested_model,
        "actual_model": effective_model or None,
        "text": response_text,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "token_usage": parse_codex_jsonl_usage(result.stdout or ""),
    }


def claude_missing_resume_session(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    return "No conversation found with session ID" in text


def parse_claude_usage(payload: dict[str, Any]) -> dict[str, int] | None:
    usage = empty_token_usage()
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict):
        for item in model_usage.values():
            if not isinstance(item, dict):
                continue
            usage["input_tokens"] += int(item.get("inputTokens") or 0)
            usage["cached_input_tokens"] += int(item.get("cacheReadInputTokens") or 0)
            usage["cached_input_tokens"] += int(item.get("cacheCreationInputTokens") or 0)
            usage["output_tokens"] += int(item.get("outputTokens") or 0)

    raw_usage = payload.get("usage")
    if not any(usage.values()) and isinstance(raw_usage, dict):
        input_tokens = int(raw_usage.get("input_tokens") or 0)
        cache_read_tokens = int(raw_usage.get("cache_read_input_tokens") or 0)
        cache_create_tokens = int(raw_usage.get("cache_creation_input_tokens") or 0)
        output_tokens = int(raw_usage.get("output_tokens") or 0)
        usage["input_tokens"] += input_tokens
        usage["cached_input_tokens"] += cache_read_tokens + cache_create_tokens
        usage["output_tokens"] += output_tokens

    if not any(usage.values()):
        return None
    usage["total_tokens"] = (
        usage["input_tokens"]
        + usage["cached_input_tokens"]
        + usage["output_tokens"]
        + usage["reasoning_output_tokens"]
    )
    return usage


def parse_claude_result_payload(stdout: str) -> dict[str, Any] | None:
    raw = stdout.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_claude_result_json(stdout: str) -> tuple[str, str | None, dict[str, int] | None]:
    raw = stdout.strip()
    payload = parse_claude_result_payload(raw)
    if payload is None:
        return raw, None, None
    response_text = str(payload.get("result") or "").strip()
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip() or None
    return response_text, session_id, parse_claude_usage(payload)


def claude_json_error_summary(stdout: str) -> str | None:
    payload = parse_claude_result_payload(stdout)
    if not payload or not payload.get("is_error"):
        return None
    result = str(payload.get("result") or "").strip()
    api_status = payload.get("api_error_status")
    if api_status:
        prefix = f"Claude API error {api_status}"
        return f"{prefix}: {result}" if result else prefix
    return result or "Claude returned an error result"


def claude_should_retry_without_model(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    error_summary = claude_json_error_summary(result.stdout or "")
    if error_summary:
        text = f"{error_summary}\n{text}"
    auth_markers = (
        "authentication_failed",
        "Failed to authenticate",
        "api_error_status\": 401",
        "api_error_status 401",
        "Claude API error 401",
        "该令牌已过期",
    )
    if any(marker in text for marker in auth_markers):
        return False
    retry_markers = (
        "api_error_status",
        "无可用渠道",
        "当前分组",
        "model",
        "503",
    )
    return any(marker in text for marker in retry_markers)


def run_claude_chat_turn(
    config: dict[str, Any],
    work_dir: Path,
    prompt: str,
    *,
    resume_session_id: str | None = None,
    log_dir: Path | None = None,
    selected_model: str | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    effective_model = str(selected_model or config.get("claude_model") or "").strip()
    requested_model = str(selected_model or "").strip() or None
    attempted_resume = bool(resume_session_id)

    def build_command(resume_id: str | None, model: str | None) -> list[str]:
        command = [
            config["claude_bin"],
            "--print",
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
        if model:
            command.extend(["--model", model])
        if resume_id:
            command.extend(["--resume", str(resume_id)])
        return command

    command = build_command(resume_session_id, effective_model or None)
    result = run_command(
        command,
        cwd=work_dir,
        env=env,
        timeout=int(config["claude_timeout"]) + 120,
    )
    if result.returncode != 0 and attempted_resume and claude_missing_resume_session(result):
        if log_dir is not None:
            (log_dir / "claude.resume.stdout.log").write_text(result.stdout or "", encoding="utf-8")
            (log_dir / "claude.resume.stderr.log").write_text(result.stderr or "", encoding="utf-8")
        command = build_command(None, effective_model or None)
        result = run_command(
            command,
            cwd=work_dir,
            env=env,
            timeout=int(config["claude_timeout"]) + 120,
        )
    if effective_model and (
        result.returncode != 0 or claude_json_error_summary(result.stdout or "") is not None
    ) and claude_should_retry_without_model(result):
        print(
            f"warning: claude selected model {effective_model} failed; retrying with Claude Code default model",
            file=sys.stderr,
        )
        if log_dir is not None:
            (log_dir / "claude.selected-model.stdout.log").write_text(result.stdout or "", encoding="utf-8")
            (log_dir / "claude.selected-model.stderr.log").write_text(result.stderr or "", encoding="utf-8")
        command = build_command(None, None)
        result = run_command(
            command,
            cwd=work_dir,
            env=env,
            timeout=int(config["claude_timeout"]) + 120,
        )
        effective_model = str(config.get("claude_model") or "").strip()
    if log_dir is not None:
        (log_dir / "claude.stdout.log").write_text(result.stdout or "", encoding="utf-8")
        (log_dir / "claude.stderr.log").write_text(result.stderr or "", encoding="utf-8")
    error_summary = claude_json_error_summary(result.stdout or "")
    if result.returncode != 0 or error_summary is not None:
        stderr_summary = tail_text(result.stderr or "", 2000)
        stdout_summary = tail_text(result.stdout or "", 2000)
        details: list[str] = []
        if error_summary:
            details.append(error_summary)
        if stdout_summary:
            details.append(stdout_summary)
        if stderr_summary:
            details.append(stderr_summary)
        raise RuntimeError(
            "claude execution failed\n"
            + "\n".join(details)
        )
    response_text, claude_session_id, token_usage = parse_claude_result_json(result.stdout or "")
    if not response_text:
        raise RuntimeError("claude returned empty assistant reply")
    return {
        "agent_id": "claude",
        "backend": "claude",
        "agent_session_id": claude_session_id or resume_session_id,
        "requested_model": requested_model,
        "actual_model": effective_model or None,
        "text": response_text,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "token_usage": token_usage,
    }


def run_discussion_turn(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    work_dir: Path,
    prompt: str,
    session_key: str,
    *,
    agent_session_id: str | None = None,
    log_dir: Path | None = None,
    selected_model: str | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    effective_backend = str(backend or config.get("execution_backend") or "codex").strip().lower()
    if effective_backend == "codex":
        return run_codex_chat_turn(
            config,
            work_dir,
            prompt,
            resume_session_id=agent_session_id,
            log_dir=log_dir,
            selected_model=selected_model,
        )
    if effective_backend == "claude":
        return run_claude_chat_turn(
            config,
            work_dir,
            prompt,
            resume_session_id=agent_session_id,
            log_dir=log_dir,
            selected_model=selected_model,
        )
    raise RuntimeError(f"unsupported discussion backend: {effective_backend}")


def run_executor(
    config: dict[str, Any],
    repo_full_name: str,
    issue_number: int,
    work_dir: Path,
    prompt: str,
    job_dir: Path,
    session_key: str,
    agent_session_id: str | None = None,
    selected_model: str | None = None,
    backend: str | None = None,
) -> dict[str, str]:
    effective_backend = str(backend or config.get("execution_backend") or "codex").strip().lower()
    if effective_backend == "codex":
        turn = run_codex_chat_turn(
            config,
            work_dir,
            prompt,
            resume_session_id=agent_session_id,
            log_dir=job_dir,
            selected_model=selected_model,
        )
        response_text = str(turn["text"])
        stderr_text = str(turn.get("stderr") or "")
        try:
            parsed = parse_executor_result(response_text)
        except RuntimeError as exc:
            parts = [
                "codex final response missing structured result",
                "assistant reply:",
                tail_text(response_text, 1200),
            ]
            if stderr_text.strip():
                parts.extend(["stderr:", tail_text(stderr_text, 1200)])
            raise RuntimeError("\n".join(parts).strip()) from exc
        parsed["agent_id"] = str(turn["agent_id"])
        parsed["agent_session_id"] = str(turn["agent_session_id"] or agent_session_id or session_key)
        parsed["actual_backend"] = str(turn.get("backend") or effective_backend)
        parsed["requested_model"] = str(turn.get("requested_model") or selected_model or "").strip() or None
        parsed["actual_model"] = str(turn.get("actual_model") or "").strip() or None
        parsed["token_usage"] = normalize_token_usage(turn.get("token_usage"))
        return parsed
    if effective_backend == "claude":
        turn = run_claude_chat_turn(
            config,
            work_dir,
            prompt,
            resume_session_id=agent_session_id,
            log_dir=job_dir,
            selected_model=selected_model,
        )
        response_text = str(turn["text"])
        stderr_text = str(turn.get("stderr") or "")
        try:
            parsed = parse_executor_result(response_text)
        except RuntimeError as exc:
            parts = [
                "claude final response missing structured result",
                "assistant reply:",
                tail_text(response_text, 1200),
            ]
            if stderr_text.strip():
                parts.extend(["stderr:", tail_text(stderr_text, 1200)])
            raise RuntimeError("\n".join(parts).strip()) from exc
        parsed["agent_id"] = str(turn["agent_id"])
        parsed["agent_session_id"] = str(turn["agent_session_id"] or agent_session_id or session_key)
        parsed["actual_backend"] = str(turn.get("backend") or effective_backend)
        parsed["requested_model"] = str(turn.get("requested_model") or selected_model or "").strip() or None
        parsed["actual_model"] = str(turn.get("actual_model") or "").strip() or None
        parsed["token_usage"] = normalize_token_usage(turn.get("token_usage"))
        return parsed
    raise RuntimeError(f"unsupported execution backend: {effective_backend}")


def post_job_progress(
    context: WorkerContext,
    repo_full_name: str,
    issue_number: int,
    job_id: str,
    message: str,
    *,
    backend: str | None = None,
    selected_model: str | None = None,
    actual_backend: str | None = None,
    actual_model: str | None = None,
) -> None:
    try:
        context.reply_issue_progress_to_feishu(
            repo_full_name,
            issue_number,
            job_id=job_id,
            message=message,
            backend=backend,
            selected_model=selected_model,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )
    except Exception as exc:
        print(f"warning: failed to post job progress for {repo_full_name}#{issue_number}: {exc}")


def process_job(context: WorkerContext, job_id: str) -> None:
    row = context.get_job(job_id)
    if row is None:
        raise RuntimeError(f"job not found: {job_id}")
    context.mark_job_running(job_id, os.getpid())

    payload = json.loads(str(row["payload_json"]))
    payload_execution = payload.get("_execution") if isinstance(payload, dict) and isinstance(payload.get("_execution"), dict) else None
    repo_full_name = str(row["repo_full_name"])
    issue_number = int(row["issue_number"])
    issue = payload["issue"]
    issue_title = issue.get("title") or f"Issue #{issue_number}"
    owner, repo = repo_full_name.split("/", 1)
    job_dir = Path(str(row["job_dir"]))

    final_status = "failed"
    error_text: str | None = None
    result_summary: str | None = None
    pr_url: str | None = None
    token_usage = empty_token_usage()
    active_locked = False
    repo_locked = False
    selected_model: str | None = None
    session_backend: str | None = None
    actual_model: str | None = None
    actual_backend: str | None = None

    try:
        active_locked = acquire_active_lock(context.config, repo_full_name, issue_number)
        if not active_locked:
            raise RuntimeError(f"issue already active: {repo_full_name}#{issue_number}")

        repo_locked = acquire_repo_lock(context.config, repo_full_name)
        if not repo_locked:
            raise RuntimeError(f"repo lock timeout: {repo_full_name}")

        token = get_installation_token(context.config)
        repo_info = get_repo_info(context.config, token, owner, repo)
        default_branch = context.config["default_base_branch"] or str(repo_info.get("default_branch") or "main")

        session_row = context.ensure_issue_session(repo_full_name, issue_number, issue_title)
        session_key = str(session_row["session_key"])
        branch_name = str(session_row["branch_name"])
        existing_agent_session_id = str(session_row["agent_session_id"] or "").strip() or None
        live_session_backend = str(session_row["backend"] or "").strip().lower() or None
        selected_model = (
            str((payload_execution or {}).get("selected_model") or "").strip()
            or str(session_row["selected_model"] or "").strip()
            or None
        )
        session_backend = (
            str((payload_execution or {}).get("backend") or "").strip().lower()
            or str(session_row["backend"] or "").strip().lower()
            or "codex"
        )
        if live_session_backend and session_backend != live_session_backend:
            existing_agent_session_id = None
        actual_model = (
            str((payload_execution or {}).get("model") or "").strip()
            or selected_model
            or (
                str(
                    context.config["claude_model"]
                    if session_backend == "claude"
                    else context.config["codex_model"]
                ).strip()
                or None
            )
        )
        actual_backend = session_backend
        update_job_execution_metadata(
            job_dir,
            backend=session_backend,
            selected_model=selected_model,
            model=actual_model,
        )
        post_job_progress(
            context,
            repo_full_name,
            issue_number,
            job_id,
            "执行进程已启动，正在准备仓库。",
            backend=session_backend,
            selected_model=selected_model,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )
        _, work_dir = ensure_repo_checkout(context.config, repo_full_name, issue_number, default_branch, branch_name)
        post_job_progress(
            context,
            repo_full_name,
            issue_number,
            job_id,
            "已拉取并切换仓库，正在调用模型。",
            backend=session_backend,
            selected_model=selected_model,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )

        prompt = build_prompt(
            repo_full_name,
            issue,
            str(work_dir),
            context.config["test_command"],
            session_key=session_key,
        )
        executor_result = run_executor(
            context.config,
            repo_full_name,
            issue_number,
            work_dir,
            prompt,
            job_dir,
            session_key,
            existing_agent_session_id,
            selected_model,
            backend=session_backend,
        )
        actual_backend = str(executor_result.get("actual_backend") or session_backend or "").strip().lower() or session_backend
        actual_model = str(executor_result.get("actual_model") or "").strip() or actual_model
        update_job_execution_metadata(
            job_dir,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )
        post_job_progress(
            context,
            repo_full_name,
            issue_number,
            job_id,
            "已完成模型执行，正在检查代码改动。",
            backend=session_backend,
            selected_model=selected_model,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )
        final_status = str(executor_result["status"])
        result_summary = str(executor_result["text"])
        agent_session_id = str(executor_result.get("agent_session_id") or "").strip() or None
        merge_token_usage(token_usage, normalize_token_usage(executor_result.get("token_usage")))

        context.upsert_issue_session(
            repo_full_name,
            issue_number,
            backend=session_backend,
            selected_model=selected_model,
            clear_selected_model=selected_model is None and session_backend is not None,
            session_state="active",
            agent_session_id=agent_session_id,
            summary=result_summary,
            last_result_status=final_status,
        )

        if final_status == "succeeded":
            if not repo_has_commit_worthy_changes(work_dir):
                post_job_progress(
                    context,
                    repo_full_name,
                    issue_number,
                    job_id,
                    "模型结果没有产生可提交改动，正在追加一次修复执行。",
                    backend=session_backend,
                    selected_model=selected_model,
                    actual_backend=actual_backend,
                    actual_model=actual_model,
                )
                retry_prompt = build_missing_changes_retry_prompt(str(work_dir), result_summary)
                retry_job_dir = ensure_dir(job_dir / "retry-no-diff")
                retry_result = run_executor(
                    context.config,
                    repo_full_name,
                    issue_number,
                    work_dir,
                    retry_prompt,
                    retry_job_dir,
                    session_key,
                    agent_session_id,
                    selected_model,
                    backend=session_backend,
                )
                final_status = str(retry_result["status"])
                result_summary = str(retry_result["text"])
                agent_session_id = str(retry_result.get("agent_session_id") or "").strip() or agent_session_id
                actual_backend = str(retry_result.get("actual_backend") or session_backend or "").strip().lower() or session_backend
                actual_model = str(retry_result.get("actual_model") or "").strip() or actual_model
                update_job_execution_metadata(
                    job_dir,
                    actual_backend=actual_backend,
                    actual_model=actual_model,
                )
                merge_token_usage(token_usage, normalize_token_usage(retry_result.get("token_usage")))
                context.upsert_issue_session(
                    repo_full_name,
                    issue_number,
                    backend=session_backend,
                    selected_model=selected_model,
                    clear_selected_model=selected_model is None and session_backend is not None,
                    session_state="active",
                    agent_session_id=agent_session_id,
                    summary=result_summary,
                    last_result_status=final_status,
                )
                if final_status == "succeeded" and not repo_has_commit_worthy_changes(work_dir):
                    raise RuntimeError(
                        "executor claimed succeeded but repository still has no commit-worthy changes after retry"
                    )

        if final_status == "succeeded":
            post_job_progress(
                context,
                repo_full_name,
                issue_number,
                job_id,
                "已检测到代码改动，正在提交分支并创建 PR。",
                backend=session_backend,
                selected_model=selected_model,
                actual_backend=actual_backend,
                actual_model=actual_model,
            )
            pr_title = f"{context.config['pr_title_prefix']} {issue_title}".strip()
            publish_result = publish_pull_request_via_git_push_and_api(
                context.config,
                token,
                work_dir,
                owner,
                repo,
                issue_number,
                issue_title,
                default_branch,
                pr_title,
                result_summary,
            )
            pr_url = str(publish_result["html_url"])
            post_job_progress(
                context,
                repo_full_name,
                issue_number,
                job_id,
                f"PR 已创建：{pr_url}",
                backend=session_backend,
                selected_model=selected_model,
                actual_backend=actual_backend,
                actual_model=actual_model,
            )
            context.upsert_issue_session(
                repo_full_name,
                issue_number,
                backend=session_backend,
                selected_model=selected_model,
                clear_selected_model=selected_model is None and session_backend is not None,
                session_state="done",
                agent_session_id=agent_session_id,
                pr_url=pr_url,
                summary=result_summary,
                last_result_status=final_status,
            )
            comment_lines = [
                f"{service_actor_name()} 已创建 PR：{pr_url}",
                "",
                result_summary,
            ]
            if context.config["submit_comment_after_pr"] and context.config["submit_comment_body"]:
                comment_lines.extend(["", context.config["submit_comment_body"]])
            comment_issue(context.config, token, owner, repo, issue_number, "\n".join(comment_lines).strip())
            try:
                from src.clients.github_client import set_issue_status_label
                set_issue_status_label(
                    context.config, token, owner, repo, issue_number,
                    "issue_label_pr_ready_name",
                )
            except Exception as exc:
                print(f"warning: failed to set issue label for {repo_full_name}#{issue_number}: {exc}")
            result_summary = f"{result_summary}\n\nPR: {pr_url}"
        elif final_status == "no_change":
            context.upsert_issue_session(
                repo_full_name,
                issue_number,
                backend=session_backend,
                selected_model=selected_model,
                clear_selected_model=selected_model is None and session_backend is not None,
                session_state="done",
                agent_session_id=agent_session_id,
                summary=result_summary,
                last_result_status=final_status,
            )
        else:
            context.upsert_issue_session(
                repo_full_name,
                issue_number,
                backend=session_backend,
                selected_model=selected_model,
                clear_selected_model=selected_model is None and session_backend is not None,
                session_state="failed",
                agent_session_id=agent_session_id,
                summary=result_summary,
                last_result_status=final_status,
            )
    except Exception:
        raw_error_text = tail_text(traceback.format_exc(), 4000)
        error_text = user_friendly_error_summary(raw_error_text)
        final_status = "failed"
        context.upsert_issue_session(
            repo_full_name,
            issue_number,
            backend=session_backend,
            selected_model=selected_model,
            clear_selected_model=selected_model is None and session_backend is not None,
            session_state="failed",
            summary=raw_error_text,
            last_result_status="failed",
        )
        raise
    finally:
        final_token_usage = normalize_token_usage(token_usage)
        write_job_token_usage(job_dir, final_token_usage)
        if final_token_usage is not None:
            print(f"[worker] job {job_id} token usage: {json.dumps(final_token_usage, ensure_ascii=True, sort_keys=True)}")
        if final_status == "failed":
            try:
                context.upsert_issue_session(
                    repo_full_name,
                    issue_number,
                    backend=session_backend,
                    selected_model=selected_model,
                    clear_selected_model=selected_model is None and session_backend is not None,
                    session_state="failed",
                    summary=error_text or result_summary,
                    last_result_status="failed",
                )
            except Exception as exc:
                print(f"warning: failed to reconcile failed issue session for {repo_full_name}#{issue_number}: {exc}")
        context.mark_job_finished(
            job_id,
            final_status,
            error_text=error_text,
            result_summary=result_summary,
        )
        context.clear_issue_active_job(repo_full_name, issue_number, job_id)
        if active_locked:
            release_active_lock(context.config, repo_full_name, issue_number)
        if repo_locked:
            release_repo_lock(context.config, repo_full_name)
        context.cleanup_closed_issue_if_finished(repo_full_name, issue_number)
        context.reply_issue_execution_result_to_feishu(
            repo_full_name,
            issue_number,
            job_id=job_id,
            status=final_status,
            pr_url=pr_url,
            result_summary=result_summary,
            error_text=error_text,
            token_usage=final_token_usage,
            backend=session_backend,
            selected_model=selected_model,
            actual_backend=actual_backend,
            actual_model=actual_model,
        )


def spawn_worker(config: dict[str, Any], job_id: str, job_dir: Path) -> int:
    log_file = job_dir / "worker.log"
    with log_file.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "src",
                "--env-file",
                config["env_file"],
                "run-job",
                job_id,
            ],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return process.pid


__all__ = [
    "WorkerContext",
    "acquire_active_lock",
    "acquire_repo_lock",
    "active_lock_path",
    "build_issue_agent_id",
    "build_missing_changes_retry_prompt",
    "build_prompt",
    "ensure_repo_checkout",
    "publish_pull_request_via_git_push_and_api",
    "release_active_lock",
    "release_repo_lock",
    "repo_has_commit_worthy_changes",
    "repo_checkout_dir",
    "repo_issue_root",
    "repo_lock_path",
    "process_job",
    "run_claude_chat_turn",
    "run_executor",
    "spawn_worker",
]
