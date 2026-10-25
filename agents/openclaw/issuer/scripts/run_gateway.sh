#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

load_github_env
ensure_prereqs
ensure_layout
bash "${SCRIPT_DIR}/sync_workspace.sh" "${WORKSPACE_DIR}" >/dev/null

CURRENT_PORT="$(gateway_port)"

export OPENCLAW_CONFIG_PATH
export OPENCLAW_STATE_DIR="${STATE_DIR}"
echo "$$" > "${PID_FILE}"

exec "${OPENCLAW_RUNNER}" gateway run --port "${CURRENT_PORT}" "$@"
