#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
CONFIG_DIR="${CONFIG_DIR:-${APP_DIR}/config}"
DATA_DIR="${DATA_DIR:-${APP_DIR}/data}"
LOG_DIR="${LOG_DIR:-${DATA_DIR}/logs}"
OPENCLAW_DIR="${OPENCLAW_DIR:-${DATA_DIR}/openclaw}"
SECRETS_DIR="${SECRETS_DIR:-${APP_DIR}/secrets}"
OPENCLAW_RUNNER="${OPENCLAW_RUNNER:-${SCRIPT_DIR}/openclaw-local}"

OPENCLAW_CONFIG_TEMPLATE="${OPENCLAW_CONFIG_TEMPLATE:-${CONFIG_DIR}/openclaw.json.template}"
OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-${CONFIG_DIR}/openclaw.json}"
GITHUB_ENV_TEMPLATE="${GITHUB_ENV_TEMPLATE:-${CONFIG_DIR}/github-app.config.env.template}"
GITHUB_ENV_FILE="${GITHUB_ENV_FILE:-${CONFIG_DIR}/github-app.config.env}"
APP_POLICY_FILE="${APP_POLICY_FILE:-${CONFIG_DIR}/policy.json}"
POLICY_TEMPLATE="${POLICY_TEMPLATE:-${CONFIG_DIR}/policy.example.json}"

WORKSPACE_DIR="${WORKSPACE_DIR:-${OPENCLAW_WORKSPACE_DIR:-${OPENCLAW_DIR}/workspace-larkbot}}"
STATE_DIR="${STATE_DIR:-${OPENCLAW_STATE_DIR:-${OPENCLAW_DIR}/state}}"
PID_FILE="${PID_FILE:-${OPENCLAW_PID_FILE:-${OPENCLAW_DIR}/openclaw-gateway.pid}}"
LOG_PATH="${LOG_PATH:-${OPENCLAW_LOG_PATH:-${LOG_DIR}/openclaw-gateway.log}}"
PORT="${PORT:-18890}"

log() {
  echo "[issuer] $*"
}

fail() {
  echo "[issuer] ERROR: $*" >&2
  exit 1
}

load_env_file() {
  local path="$1"
  python3 - "$path" <<'PY'
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}")
PY
}

load_github_env() {
  if [[ -f "${GITHUB_ENV_FILE}" ]]; then
    eval "$(load_env_file "${GITHUB_ENV_FILE}")"
  fi

  export GITHUB_APP_ENV_FILE="${GITHUB_ENV_FILE}"

  if [[ -n "${GITHUB_APP_PRIVATE_KEY_PATH:-}" && "${GITHUB_APP_PRIVATE_KEY_PATH}" != /* ]]; then
    export GITHUB_APP_PRIVATE_KEY_PATH="${APP_DIR}/${GITHUB_APP_PRIVATE_KEY_PATH}"
  fi

  if [[ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" && -n "${GITHUB_APP_PRIVATE_KEY:-}" && "${GITHUB_APP_PRIVATE_KEY}" != *BEGIN* ]]; then
    local key_path="${GITHUB_APP_PRIVATE_KEY}"
    if [[ "${key_path}" != /* ]]; then
      key_path="${APP_DIR}/${key_path}"
    fi
    if [[ -f "${key_path}" ]]; then
      export GITHUB_APP_PRIVATE_KEY_PATH="${key_path}"
    fi
  fi
}

detect_openclaw_bin() {
  if [[ -n "${OPENCLAW_BIN:-}" && -x "${OPENCLAW_BIN}" ]]; then
    echo "${OPENCLAW_BIN}"
    return 0
  fi
  for candidate in /usr/local/bin/openclaw /usr/local/node-v22.22.0-linux-x64/bin/openclaw; do
    if [[ -x "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

gateway_port() {
  if [[ -f "${OPENCLAW_CONFIG_PATH}" ]]; then
    python3 - "${OPENCLAW_CONFIG_PATH}" "${PORT}" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
default_port = sys.argv[2]
payload = json.loads(config_path.read_text(encoding="utf-8"))
print((payload.get("gateway") or {}).get("port") or default_port)
PY
  else
    printf '%s\n' "${PORT}"
  fi
}

ensure_layout() {
  mkdir -p \
    "${CONFIG_DIR}" \
    "${SECRETS_DIR}" \
    "${OPENCLAW_DIR}" \
    "${STATE_DIR}" \
    "${WORKSPACE_DIR}" \
    "${LOG_DIR}" \
    "$(dirname "${PID_FILE}")" \
    "$(dirname "${LOG_PATH}")"
}

ensure_prereqs() {
  [[ -x "${OPENCLAW_RUNNER}" ]] || fail "OpenClaw runner not found: ${OPENCLAW_RUNNER}"
  [[ -f "${OPENCLAW_CONFIG_PATH}" ]] || fail "missing OpenClaw config: ${OPENCLAW_CONFIG_PATH}"
}

find_running_pids() {
  for p in $(pgrep -x openclaw 2>/dev/null || true); do
    if [[ -r "/proc/$p/environ" ]] && tr '\0' '\n' < "/proc/$p/environ" | grep -q "^OPENCLAW_CONFIG_PATH=${OPENCLAW_CONFIG_PATH}$"; then
      echo "$p"
    fi
  done
}

is_port_ready() {
  local port="$1"
  ss -lntp 2>/dev/null | grep -qE "127.0.0.1:${port}|\[::1\]:${port}"
}

port_listener_lines() {
  local port="$1"
  ss -lntp 2>/dev/null | grep -E "127.0.0.1:${port}|\[::1\]:${port}" || true
}
