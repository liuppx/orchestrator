#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-start}"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [start|stop|restart]

No argument defaults to: start
USAGE
}

load_env() {
  local primary="$ROOT_DIR/config/hub.env"
  local template="$ROOT_DIR/config/hub.env.template"
  local loaded=""
  local has_repo_root=0
  local has_runtime_dir=0
  local has_instances_root=0
  local has_bind_addr=0
  local has_python_app_dir=0
  local env_repo_root=""
  local env_runtime_dir=""
  local env_instances_root=""
  local env_bind_addr=""
  local env_python_app_dir=""

  if [[ ${HUB_REPO_ROOT+x} ]]; then
    has_repo_root=1
    env_repo_root="$HUB_REPO_ROOT"
  fi
  if [[ ${HUB_RUNTIME_DIR+x} ]]; then
    has_runtime_dir=1
    env_runtime_dir="$HUB_RUNTIME_DIR"
  fi
  if [[ ${HUB_INSTANCES_ROOT+x} ]]; then
    has_instances_root=1
    env_instances_root="$HUB_INSTANCES_ROOT"
  fi
  if [[ ${HUB_BIND_ADDR+x} ]]; then
    has_bind_addr=1
    env_bind_addr="$HUB_BIND_ADDR"
  fi
  if [[ ${HUB_PYTHON_APP_DIR+x} ]]; then
    has_python_app_dir=1
    env_python_app_dir="$HUB_PYTHON_APP_DIR"
  fi

  if [[ -f "$primary" ]]; then
    loaded="$primary"
  fi

  if [[ -n "$loaded" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$loaded"
    set +a
  else
    if [[ -f "$template" ]]; then
      echo "[warn] config file not found: $primary"
      echo "[warn] copy template and edit it: cp $template $primary"
    else
      echo "[warn] config file not found, and template missing: $template"
    fi
  fi

  export HUB_REPO_ROOT="${HUB_REPO_ROOT:-$ROOT_DIR}"
  export HUB_RUNTIME_DIR="${HUB_RUNTIME_DIR:-$ROOT_DIR/runtime/control-plane}"
  export HUB_INSTANCES_ROOT="${HUB_INSTANCES_ROOT:-$ROOT_DIR/runtime/instances}"
  export HUB_BIND_ADDR="${HUB_BIND_ADDR:-127.0.0.1:3900}"
  export HUB_PYTHON_APP_DIR="${HUB_PYTHON_APP_DIR:-$ROOT_DIR/hub/backend}"

  if [[ "$has_repo_root" == "1" ]]; then
    export HUB_REPO_ROOT="$env_repo_root"
  fi
  if [[ "$has_runtime_dir" == "1" ]]; then
    export HUB_RUNTIME_DIR="$env_runtime_dir"
  fi
  if [[ "$has_instances_root" == "1" ]]; then
    export HUB_INSTANCES_ROOT="$env_instances_root"
  fi
  if [[ "$has_bind_addr" == "1" ]]; then
    export HUB_BIND_ADDR="$env_bind_addr"
  fi
  if [[ "$has_python_app_dir" == "1" ]]; then
    export HUB_PYTHON_APP_DIR="$env_python_app_dir"
  fi
}

start_command_desc() {
  if [[ -n "${HUB_CONTROL_PLANE_CMD:-}" ]]; then
    echo "$HUB_CONTROL_PLANE_CMD"
  elif command -v uv >/dev/null 2>&1; then
    echo "uv run agent-hub (cwd=${HUB_PYTHON_APP_DIR})"
  elif [[ -x "${HUB_PYTHON_APP_DIR}/.venv/bin/agent-hub" ]]; then
    echo "${HUB_PYTHON_APP_DIR}/.venv/bin/agent-hub"
  else
    echo "[error] Python control plane not ready. Run scripts/bootstrap_full_stack.sh first." >&2
    exit 1
  fi
}

start_command_shell() {
  if [[ -n "${HUB_CONTROL_PLANE_CMD:-}" ]]; then
    printf 'export HUB_REPO_ROOT=%q; export HUB_RUNTIME_DIR=%q; export HUB_INSTANCES_ROOT=%q; export HUB_BIND_ADDR=%q; cd %q && exec %s' \
      "$HUB_REPO_ROOT" \
      "$HUB_RUNTIME_DIR" \
      "$HUB_INSTANCES_ROOT" \
      "$HUB_BIND_ADDR" \
      "$HUB_PYTHON_APP_DIR" \
      "$HUB_CONTROL_PLANE_CMD"
  elif command -v uv >/dev/null 2>&1; then
    printf 'export HUB_REPO_ROOT=%q; export HUB_RUNTIME_DIR=%q; export HUB_INSTANCES_ROOT=%q; export HUB_BIND_ADDR=%q; cd %q && exec uv run agent-hub' \
      "$HUB_REPO_ROOT" \
      "$HUB_RUNTIME_DIR" \
      "$HUB_INSTANCES_ROOT" \
      "$HUB_BIND_ADDR" \
      "$HUB_PYTHON_APP_DIR"
  else
    printf 'export HUB_REPO_ROOT=%q; export HUB_RUNTIME_DIR=%q; export HUB_INSTANCES_ROOT=%q; export HUB_BIND_ADDR=%q; exec %q' \
      "$HUB_REPO_ROOT" \
      "$HUB_RUNTIME_DIR" \
      "$HUB_INSTANCES_ROOT" \
      "$HUB_BIND_ADDR" \
      "${HUB_PYTHON_APP_DIR}/.venv/bin/agent-hub"
  fi
}

python_launcher() {
  local candidates=(
    "${HUB_PYTHON_APP_DIR}/.venv/bin/python3"
    "${HUB_PYTHON_APP_DIR}/.venv/bin/python"
  )
  local p
  for p in "${candidates[@]}"; do
    if [[ -x "$p" ]]; then
      echo "$p"
      return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  echo "[error] python launcher not found" >&2
  exit 1
}

spawn_detached() {
  local start_shell="$1"
  local log_file="$2"
  local launcher
  launcher="$(python_launcher)"

  START_SHELL="$start_shell" LOG_FILE="$log_file" "$launcher" - <<'PY'
import os
import subprocess
import sys

start_shell = os.environ["START_SHELL"]
log_file = os.environ["LOG_FILE"]

with open(log_file, "ab", buffering=0) as log_fp, open(os.devnull, "rb") as null_in:
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", start_shell],
        stdin=null_in,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )

print(proc.pid)
PY
}

runtime_pid_file() {
  echo "${HUB_RUNTIME_DIR}/control-plane.pid"
}

runtime_log_file() {
  echo "${HUB_RUNTIME_DIR}/logs/control-plane.out.log"
}

ensure_runtime_dirs() {
  mkdir -p "${HUB_RUNTIME_DIR}/logs" "${HUB_INSTANCES_ROOT}"
}

is_pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

find_listener_pid() {
  local port="$1"

  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1
    return 0
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $NF}' | sed -E 's/.*pid=([0-9]+).*/\1/' | head -n 1
    return 0
  fi
}

is_port_in_use() {
  local port="$1"

  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN -n -P >/dev/null 2>&1
    return $?
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk '{print $4}' | grep -E "[:.]${port}$" >/dev/null 2>&1
    return $?
  fi

  return 1
}

wait_pid_exit() {
  local pid="$1"
  local max_wait="${2:-10}"
  local i
  for ((i=0; i<max_wait; i++)); do
    if ! is_pid_alive "$pid"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_service() {
  load_env
  ensure_runtime_dirs

  local pid_file
  pid_file="$(runtime_pid_file)"

  local log_file
  log_file="$(runtime_log_file)"

  local start_desc
  start_desc="$(start_command_desc)"

  local start_shell
  start_shell="$(start_command_shell)"

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file" || true)"
    if is_pid_alive "$old_pid"; then
      echo "[ok] already running, pid=$old_pid"
      echo "[url] http://${HUB_BIND_ADDR}/"
      return 0
    fi
  fi

  local port
  port="${HUB_BIND_ADDR##*:}"
  if is_port_in_use "$port"; then
    local listener_pid
    listener_pid="$(find_listener_pid "$port" || true)"
    if is_pid_alive "$listener_pid"; then
      echo "$listener_pid" > "$pid_file"
      echo "[ok] port $port already served by pid=$listener_pid"
      echo "[url] http://${HUB_BIND_ADDR}/"
      return 0
    fi
    echo "[error] port $port is already in use; set HUB_BIND_ADDR or stop conflicting process" >&2
    return 1
  fi

  echo "[info] starting control plane (python): $start_desc"
  local pid
  pid="$(spawn_detached "$start_shell" "$log_file")"
  echo "$pid" > "$pid_file"

  sleep 1
  if is_pid_alive "$pid"; then
    echo "[ok] started pid=$pid bind=${HUB_BIND_ADDR}"
    echo "[log] $log_file"
    echo "[url] http://${HUB_BIND_ADDR}/"
    echo "[health] curl -sS http://${HUB_BIND_ADDR}/api/v1/public/health"
  else
    echo "[error] start failed, see log: $log_file" >&2
    return 1
  fi
}

stop_service() {
  load_env

  local pid_file
  pid_file="$(runtime_pid_file)"

  if [[ ! -f "$pid_file" ]]; then
    echo "[ok] no pid file, already stopped"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" || true)"

  if ! is_pid_alive "$pid"; then
    rm -f "$pid_file"
    echo "[ok] process already gone"
    return 0
  fi

  kill "$pid" || true
  if ! wait_pid_exit "$pid" 8; then
    echo "[warn] graceful stop timeout, force killing pid=$pid"
    kill -9 "$pid" || true
  fi

  rm -f "$pid_file"
  echo "[ok] stopped pid=$pid"
}

restart_service() {
  stop_service
  start_service
}

case "$ACTION" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    restart_service
    ;;
  *)
    usage
    exit 1
    ;;
esac
