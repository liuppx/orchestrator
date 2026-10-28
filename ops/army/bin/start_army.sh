#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

if [[ ! -f "${ARMY_ROOT}/army.env" ]]; then
  cp "${ARMY_ROOT}/army.env.template" "${ARMY_ROOT}/army.env"
fi

"${SCRIPT_DIR}/watchdog.sh" || true

if pgrep -f "${BOT_ROOT}/ops/army/bin/scheduler.sh" >/dev/null 2>&1; then
  echo "scheduler already running"
else
  nohup "${SCRIPT_DIR}/scheduler.sh" >>"${SCHEDULER_LOG}" 2>&1 &
  echo $! > "${ARMY_STATE_DIR}/scheduler.pid"
  echo "scheduler started pid=$!"
fi

if pgrep -f "${BOT_ROOT}/ops/army/bin/guard_loop.sh" >/dev/null 2>&1; then
  echo "guard_loop already running"
else
  nohup "${SCRIPT_DIR}/guard_loop.sh" >>"${LOG_DIR}/guard-loop.log" 2>&1 &
  echo $! > "${ARMY_STATE_DIR}/guard.pid"
  echo "guard_loop started pid=$!"
fi

"${SCRIPT_DIR}/worker_observe.sh" || true
"${SCRIPT_DIR}/worker_verify.sh" || true
"${SCRIPT_DIR}/worker_codex_advisor.sh" || true
