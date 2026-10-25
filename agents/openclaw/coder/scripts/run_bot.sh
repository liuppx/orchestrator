#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${APP_DIR}/config/coder-bot.env}"
USER_HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"

load_env_file() {
  local path="$1"
  python3 - "$path" <<'PY'
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}")
PY
}

if [[ -f "${ENV_FILE}" ]]; then
  eval "$(load_env_file "${ENV_FILE}")"
fi

export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "${UV_BIN}" ]]; then
  UV_BIN="${USER_HOME}/.local/bin/uv"
fi
if [[ -z "${UV_BIN}" ]]; then
  echo "uv not found. Install uv first or set UV_BIN=/path/to/uv." >&2
  exit 1
fi

exec "${UV_BIN}" run --frozen gunicorn -c "${APP_DIR}/config/gunicorn.conf.py" src.main:APP
