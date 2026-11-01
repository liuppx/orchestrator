#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
backend_dir="$repo_root/backend"
hub_cli="$backend_dir/.venv/bin/orchestrator-machine-hub"

if [[ ! -x "$hub_cli" ]]; then
  printf '[error] Machine Hub CLI is not installed. Run: (cd %q && uv sync --locked)\n' "$backend_dir" >&2
  exit 1
fi

cd "$backend_dir"
if [[ "${1:-}" == "--check" ]]; then
  shift
  exec "$hub_cli" --check "$@"
fi

"$hub_cli" --check "$@"
exec "$hub_cli" "$@"
