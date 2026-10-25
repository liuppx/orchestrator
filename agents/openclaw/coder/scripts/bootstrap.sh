#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/config/coder-bot.env}"
ENV_TEMPLATE="${ENV_TEMPLATE:-${APP_DIR}/config/coder-bot.env.template}"
SERVICE_NAME="${SERVICE_NAME:-coder-bot}"
BOT_USER="${BOT_USER:-$(id -un)}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/coder-bot-uv-cache}"
INSTALL_SYSTEMD="${INSTALL_SYSTEMD:-true}"

log() {
  echo "[bootstrap] $*"
}

fail() {
  echo "[bootstrap] ERROR: $*" >&2
  exit 1
}

require_file() {
  local path="$1"
  [[ -f "${path}" ]] || fail "file not found: ${path}"
}

if [[ -z "${UV_BIN}" ]]; then
  fail "uv not found. Install uv first or set UV_BIN=/path/to/uv."
fi

require_file "${APP_DIR}/pyproject.toml"

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ -f "${ENV_TEMPLATE}" ]]; then
    mkdir -p "$(dirname "${ENV_FILE}")"
    cp "${ENV_TEMPLATE}" "${ENV_FILE}"
    log "created ${ENV_FILE} from ${ENV_TEMPLATE}"
    log "fill in the required values in ${ENV_FILE}, then rerun bootstrap"
    exit 1
  fi
  fail "env file not found: ${ENV_FILE}"
fi

log "syncing dependencies with uv"
"${UV_BIN}" sync --frozen --directory "${APP_DIR}"

log "checking ${ENV_FILE}"
python3 - "${ENV_FILE}" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
values: dict[str, str] = {}
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")

required = [
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_PRIVATE_KEY_PATH",
    "GITHUB_FORK_OWNER",
    "ALLOWED_REPOS",
    "GITHUB_CLONE_SSH_KEY_PATH",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
]

placeholder_values = {
    "your-github-name",
    "upstream-owner/repo-name",
}

errors: list[str] = []
for key in required:
    value = values.get(key, "").strip()
    if not value:
        errors.append(f"{key} is empty")
    elif value in placeholder_values:
        errors.append(f"{key} still uses template placeholder: {value}")

execution_backend = values.get("EXECUTION_BACKEND", "codex").strip().lower() or "codex"
feishu_handoff = [
    values.get("FEISHU_HANDOFF_CHAT_ID", "").strip(),
    values.get("FEISHU_HANDOFF_CHAT_IDS", "").strip(),
]

if execution_backend not in {"codex", "claude"}:
    errors.append(f"EXECUTION_BACKEND must be codex or claude, got: {execution_backend}")
if not any(feishu_handoff):
    errors.append("FEISHU_HANDOFF_CHAT_ID or FEISHU_HANDOFF_CHAT_IDS must be set")

if errors:
    print("env check failed:")
    for item in errors:
        print(f"- {item}")
    sys.exit(1)

print("env check passed")
PY

log "running coder-bot doctor"
(
  cd "${APP_DIR}"
  CODER_BOT_ENV_FILE="${ENV_FILE}" UV_CACHE_DIR="${UV_CACHE_DIR}" \
    "${UV_BIN}" run --frozen coder-bot --env-file "${ENV_FILE}" doctor
)

if [[ "${INSTALL_SYSTEMD}" == "true" ]]; then
  log "installing systemd service ${SERVICE_NAME}"
  (
    cd "${APP_DIR}"
    BOT_USER="${BOT_USER}" ENV_FILE="${ENV_FILE}" SERVICE_NAME="${SERVICE_NAME}" \
      UV_BIN="${UV_BIN}" ./scripts/install_systemd.sh
  )
else
  log "skipping systemd install because INSTALL_SYSTEMD=${INSTALL_SYSTEMD}"
  log "manual bot start: ENV_FILE=${ENV_FILE} ./scripts/start_bot.sh"
fi

log "bootstrap completed"
log "health check: curl -s http://127.0.0.1:9081/health"
log "service status: sudo systemctl status --no-pager ${SERVICE_NAME}"
log "service logs: tail -f ${APP_DIR}/data/logs/gunicorn.error.log"
