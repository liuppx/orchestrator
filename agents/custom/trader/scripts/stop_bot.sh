#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${PID_FILE:-${APP_DIR}/runtime/trader.pid}"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "trader is not running"
  exit 0
fi

PID="$(cat "${PID_FILE}")"
if kill -0 "${PID}" 2>/dev/null; then
  kill "${PID}"
  echo "stopped trader pid ${PID}"
else
  echo "stale pid file for ${PID}"
fi
rm -f "${PID_FILE}"
