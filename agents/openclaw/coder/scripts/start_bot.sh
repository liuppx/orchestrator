#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${APP_DIR}/data/logs"
LOG_PATH="${CODER_BOT_LOG_PATH:-${LOG_DIR}/gunicorn.error.log}"
PID_FILE="${CODER_BOT_PID_FILE:-${APP_DIR}/data/coder-bot.pid}"
NULL_FILE="${CODER_BOT_NULL_FILE:-/tmp/coder-bot.null}"
HEALTH_URL="${CODER_BOT_HEALTH_URL:-http://127.0.0.1:9081/health}"

mkdir -p "$(dirname "${PID_FILE}")" "${LOG_DIR}"
: >"${NULL_FILE}"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}" 2>"${NULL_FILE}" || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>"${NULL_FILE}"; then
    if curl -fsS "${HEALTH_URL}" >"${NULL_FILE}" 2>&1; then
      echo "already running: pid=${old_pid}"
      exit 0
    fi
  fi
  rm -f "${PID_FILE}"
fi

if command -v setsid >"${NULL_FILE}" 2>&1; then
  setsid "${SCRIPT_DIR}/run_bot.sh" <"${NULL_FILE}" >>"${LOG_PATH}" 2>&1 &
else
  "${SCRIPT_DIR}/run_bot.sh" <"${NULL_FILE}" >>"${LOG_PATH}" 2>&1 &
fi
echo $! >"${PID_FILE}"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if [[ -f "${PID_FILE}" ]]; then
    pid="$(cat "${PID_FILE}" 2>"${NULL_FILE}" || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>"${NULL_FILE}" && curl -fsS "${HEALTH_URL}" >"${NULL_FILE}" 2>&1; then
      echo "started: pid=${pid}"
      exit 0
    fi
  fi
done

echo "start failed; tail log:" >&2
tail -n 140 "${LOG_PATH}" >&2 || true
exit 1
