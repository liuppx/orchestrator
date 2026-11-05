#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base_url="${HEALTH_BASE_URL:-http://${HUB_BIND_ADDR:-127.0.0.1:3900}}"
timeout="${HEALTH_TIMEOUT:-5}"
suite=""

usage() {
  printf 'Usage: %s --suite smoke [--base-url URL] [--timeout seconds]\n' "$(basename "$0")"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --suite)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      suite="$2"
      shift 2
      ;;
    --base-url)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      base_url="$2"
      shift 2
      ;;
    --timeout)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      timeout="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unsupported argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ "$suite" == smoke ]] || { printf 'Only --suite smoke is supported.\n' >&2; exit 2; }
[[ "$timeout" =~ ^[0-9]+([.][0-9]+)?$ ]] || { printf 'Invalid timeout: %s\n' "$timeout" >&2; exit 2; }
command -v curl >/dev/null 2>&1 || { printf 'curl is required\n' >&2; exit 3; }
command -v grep >/dev/null 2>&1 || { printf 'grep is required\n' >&2; exit 3; }

base_url="${base_url%/}"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/orchestrator-smoke.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT HUP INT TERM

bash "$script_dir/health-check.sh" --level readiness --base-url "$base_url" --timeout "$timeout"

version_body="$tmp_dir/version.json"
curl --disable --silent --show-error --fail --max-time "$timeout" \
  "$base_url/api/v1/public/version" -o "$version_body"
grep -Eq '"name"[[:space:]]*:[[:space:]]*"orchestrator"' "$version_body" || {
  printf 'Version endpoint did not identify the orchestrator service\n' >&2
  exit 1
}
grep -Eq '"version"[[:space:]]*:[[:space:]]*"[^"]+"' "$version_body" || {
  printf 'Version endpoint did not return a version\n' >&2
  exit 1
}

index_body="$tmp_dir/index.html"
curl --disable --silent --show-error --fail --max-time "$timeout" "$base_url/" -o "$index_body"
grep -Eqi '<html([[:space:]>])' "$index_body" || {
  printf 'Web console root did not return an HTML document\n' >&2
  exit 1
}

auth_status="$(curl --disable --silent --show-error --max-time "$timeout" \
  -o "$tmp_dir/auth-me.json" -w '%{http_code}' "$base_url/api/v1/public/auth/me")"
[[ "$auth_status" == 401 ]] || {
  printf 'Unauthenticated auth/me request returned HTTP %s; expected 401\n' "$auth_status" >&2
  exit 1
}

printf 'orchestrator suite=smoke status=pass checks=readiness,version,web-console,auth-boundary\n'
