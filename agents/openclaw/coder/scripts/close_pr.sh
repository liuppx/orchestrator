#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/config/coder-bot.env}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/close_pr.sh <repo|owner/repo> <pr_number>

Examples:
  ./scripts/close_pr.sh router 171
  ./scripts/close_pr.sh yeying-community/router 171
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 2 ]]; then
  usage >&2
  exit 1
fi

REPO_INPUT="$1"
PR_NUMBER="$2"

if [[ ! "${PR_NUMBER}" =~ ^[0-9]+$ ]]; then
  echo "PR number must be an integer: ${PR_NUMBER}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "python3 not found" >&2
  exit 1
fi

cd "${APP_DIR}"
PYTHONHASHSEED="${PYTHONHASHSEED:-0}" \
"${PYTHON_BIN}" - "${ENV_FILE}" "${REPO_INPUT}" "${PR_NUMBER}" <<'PY'
import sys
from pathlib import Path

from src.config import load_env_file, read_config
from src.issue_service import build_repo_alias_map
from src.clients.github_client import get_installation_token, github_request


def resolve_repo(config: dict[str, object], raw_repo: str) -> str:
    value = raw_repo.strip()
    if "/" in value:
        return value

    aliases = build_repo_alias_map(config)
    alias_match = aliases.get(value.lower())
    if alias_match:
        return alias_match

    matches = [
        str(repo_full_name)
        for repo_full_name in config.get("allowed_repos", [])
        if str(repo_full_name).split("/", 1)[-1].strip().lower() == value.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"ambiguous repo name `{value}`: {', '.join(matches)}")
    raise SystemExit(f"unknown repo `{value}`")


env_file = Path(sys.argv[1]).expanduser().resolve(strict=False)
repo_input = sys.argv[2]
pr_number = int(sys.argv[3])

load_env_file(env_file)
config = read_config(env_file)
repo_full_name = resolve_repo(config, repo_input)

allowed_repos = {str(item) for item in config.get("allowed_repos", [])}
if repo_full_name not in allowed_repos:
    raise SystemExit(f"repo `{repo_full_name}` is not in ALLOWED_REPOS")

owner, repo = repo_full_name.split("/", 1)
token = get_installation_token(config)
payload = github_request(
    config,
    "PATCH",
    f"/repos/{owner}/{repo}/pulls/{pr_number}",
    token=token,
    json_body={"state": "closed"},
).json()

print(f"closed PR #{pr_number}")
print(f"repo: {repo_full_name}")
print(f"state: {payload.get('state')}")
print(f"url: {payload.get('html_url')}")
PY
