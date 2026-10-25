#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

load_github_env
ensure_layout

CURRENT_PORT="$(gateway_port)"
OPENCLAW_BIN="$(detect_openclaw_bin || true)"

echo "app_dir=${APP_DIR}"
echo "config_path=${OPENCLAW_CONFIG_PATH}"
echo "state_dir=${STATE_DIR}"
echo "workspace_dir=${WORKSPACE_DIR}"
echo "log_path=${LOG_PATH}"
echo "pid_file=${PID_FILE}"
echo "port=${CURRENT_PORT}"

if [[ ! -f "${OPENCLAW_CONFIG_PATH}" ]]; then
  echo "missing config: ${OPENCLAW_CONFIG_PATH}"
  echo "run: ${SCRIPT_DIR}/bootstrap.sh"
  exit 1
fi

echo "== process (by config path) =="
found=0
for p in $(find_running_pids || true); do
  found=1
  echo "openclaw pid=${p}"
  ps -fp "${p}" || true
done
if [[ "${found}" -eq 0 ]]; then
  if is_port_ready "${CURRENT_PORT}"; then
    echo "openclaw pid not visible from this shell, but port ${CURRENT_PORT} is listening"
  else
    echo "openclaw not running"
  fi
fi

echo "== port =="
port_listener_lines "${CURRENT_PORT}"

echo "== github app env =="
echo "github_env_file=${GITHUB_ENV_FILE}"
echo "GITHUB_APP_ID=${GITHUB_APP_ID:-<unset>}"
echo "GITHUB_APP_INSTALLATION_ID=${GITHUB_APP_INSTALLATION_ID:-<unset>}"
echo "GITHUB_DEFAULT_OWNER=${GITHUB_DEFAULT_OWNER:-${GITHUB_OWNER:-<unset>}}"
echo "GITHUB_DEFAULT_REPO=${GITHUB_DEFAULT_REPO:-${GITHUB_REPO:-<unset>}}"
echo "GITHUB_APP_PRIVATE_KEY_PATH=${GITHUB_APP_PRIVATE_KEY_PATH:-<unset>}"

if [[ -x "${OPENCLAW_RUNNER}" ]]; then
  echo "== hooks =="
  OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH}" OPENCLAW_STATE_DIR="${STATE_DIR}" "${OPENCLAW_RUNNER}" hooks list 2>/dev/null || true
fi

echo "== recent log =="
tail -n 120 "${LOG_PATH}" 2>/dev/null || true
