#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

load_github_env
ensure_prereqs
ensure_layout

CURRENT_PORT="$(gateway_port)"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null && is_port_ready "${CURRENT_PORT}"; then
    echo "already running: pid=${old_pid}"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

pids="$(find_running_pids || true)"
if [[ -n "${pids}" ]] && is_port_ready "${CURRENT_PORT}"; then
  echo "already running: pids=${pids}"
  exit 0
fi

if [[ -z "${pids}" ]] && is_port_ready "${CURRENT_PORT}"; then
  echo "already running: port=${CURRENT_PORT} pid=not-visible"
  echo "note: current shell cannot see the host openclaw process; use stop_gateway.sh or fuser from the host shell before restarting."
  exit 0
fi

if [[ -n "${pids}" ]]; then
  for p in ${pids}; do
    kill "${p}" 2>/dev/null || true
  done
  sleep 1
fi

nohup bash "${SCRIPT_DIR}/run_gateway.sh" >> "${LOG_PATH}" 2>&1 &
echo $! > "${PID_FILE}"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if [[ -f "${PID_FILE}" ]]; then
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && is_port_ready "${CURRENT_PORT}"; then
      echo "started: pid=${pid}"
      exit 0
    fi
  fi
done

echo "start failed; tail log:" >&2
tail -n 140 "${LOG_PATH}" >&2 || true
exit 1
