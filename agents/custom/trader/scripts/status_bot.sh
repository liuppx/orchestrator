#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${PID_FILE:-${APP_DIR}/runtime/trader.pid}"
STATE_FILE="${STATE_FILE:-${APP_DIR}/runtime/state.json}"

if [[ -f "${PID_FILE}" ]]; then
  PID="$(cat "${PID_FILE}")"
  if [[ -n "${PID}" ]] && kill -0 "${PID}" 2>/dev/null; then
    echo "trader running pid=${PID}"
  else
    echo "trader not running"
    rm -f "${PID_FILE}"
  fi
else
  echo "trader not running"
fi

if [[ -f "${STATE_FILE}" ]]; then
  echo "latest state:"
  cat "${STATE_FILE}"
fi
