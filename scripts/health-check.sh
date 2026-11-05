#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${HUB_ENV_FILE:-$root_dir/backend/.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

bind_addr="${HUB_BIND_ADDR:-127.0.0.1:3900}"
base_url="${HEALTH_BASE_URL:-http://${bind_addr}}"
timeout="${HEALTH_TIMEOUT:-5}"
runtime_dir="${HUB_RUNTIME_DIR:-$root_dir/runtime/control-plane}"
pid_file="${HUB_CONTROL_PLANE_PID_FILE:-$runtime_dir/control-plane.pid}"
level="readiness"

usage() {
  printf 'Usage: %s [--level liveness|readiness|all] [--base-url URL] [--timeout seconds]\n' "$(basename "$0")"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --level) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; level=$2; shift 2 ;;
    --base-url) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; base_url=$2; shift 2 ;;
    --timeout) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; timeout=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ "$level" =~ ^(liveness|readiness|all)$ ]] || { usage >&2; exit 2; }
[[ "$timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] || { printf 'Invalid timeout: %s\n' "$timeout" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { printf 'curl is required\n' >&2; exit 3; }

check_process() {
  local pid
  [[ -f "$pid_file" ]] || { printf 'Control plane pid file is missing: %s\n' "$pid_file" >&2; return 1; }
  pid="$(tr -d '[:space:]' < "$pid_file")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null || {
    printf 'Control plane process is not running\n' >&2
    return 1
  }
}

check_http() {
  curl --silent --show-error --fail --max-time "$timeout" "${base_url%/}/api/v1/public/health" >/dev/null
}

check_process || exit 1
if [[ "$level" != liveness ]]; then
  check_http || exit 1
fi
printf 'orchestrator level=%s status=pass url=%s/api/v1/public/health\n' "$level" "${base_url%/}"
