#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_PATH="${CODER_BOT_LOG_PATH:-${APP_DIR}/data/logs/gunicorn.error.log}"
PID_FILE="${CODER_BOT_PID_FILE:-${APP_DIR}/data/coder-bot.pid}"
NULL_FILE="${CODER_BOT_NULL_FILE:-/tmp/coder-bot.null}"

: >"${NULL_FILE}"

echo "instance_dir=${APP_DIR}"
echo "pid_file=${PID_FILE}"

echo "== process =="
if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" 2>"${NULL_FILE}" || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>"${NULL_FILE}"; then
    echo "coder-bot pid=${pid}"
    ps -fp "${pid}" || true
  else
    echo "pid file exists but process is not running"
  fi
else
  echo "pid file not found"
fi

echo "== port =="
ss -lntp 2>"${NULL_FILE}" | grep ":9081" || echo "bot port 9081 is not listening"

echo "== health =="
curl -s http://127.0.0.1:9081/health || echo "health unavailable"

echo ""
echo "== recent log =="
tail -n 40 "${LOG_PATH}" 2>"${NULL_FILE}" || echo "log file not found: ${LOG_PATH}"
