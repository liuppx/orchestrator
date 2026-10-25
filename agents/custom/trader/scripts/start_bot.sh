#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${APP_DIR}/config/trader.env}"
PID_FILE="${PID_FILE:-${APP_DIR}/runtime/trader.pid}"
LOG_FILE="${LOG_FILE:-${APP_DIR}/runtime/logs/launcher.log}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
VENV_BIN="${VENV_BIN:-${APP_DIR}/.venv/bin/trader-bot}"

mkdir -p "$(dirname "${PID_FILE}")" "$(dirname "${LOG_FILE}")"

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "trader already running with pid $(cat "${PID_FILE}")"
  exit 0
fi

rm -f "${PID_FILE}"

start_detached() {
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@" </dev/null >>"${LOG_FILE}" 2>&1 &
  else
    nohup "$@" </dev/null >>"${LOG_FILE}" 2>&1 &
  fi
}

(
  cd "${APP_DIR}"
  if [[ -x "${VENV_BIN}" ]]; then
    start_detached "${VENV_BIN}" run-loop --env-file "${ENV_FILE}"
  else
    if [[ -z "${UV_BIN}" ]]; then
      echo "uv not found and ${VENV_BIN} is missing" >>"${LOG_FILE}"
      exit 1
    fi
    start_detached "${UV_BIN}" run --directory "${APP_DIR}" trader-bot run-loop --env-file "${ENV_FILE}"
  fi
  echo $! > "${PID_FILE}"
  disown "$!" 2>/dev/null || true
)

sleep 1
if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "started trader pid $(cat "${PID_FILE}")"
  exit 0
fi

echo "failed to start trader" >&2
[[ -f "${LOG_FILE}" ]] && tail -n 40 "${LOG_FILE}" >&2
exit 1
