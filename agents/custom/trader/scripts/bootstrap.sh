#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${APP_DIR}/config/trader.env}"
ENV_TEMPLATE="${ENV_TEMPLATE:-${APP_DIR}/config/trader.env.template}"
STRATEGY_FILE="${STRATEGY_FILE:-${APP_DIR}/config/strategies.yaml}"
STRATEGY_TEMPLATE="${STRATEGY_TEMPLATE:-${APP_DIR}/config/strategies.example.yaml}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"

log() {
  echo "[bootstrap] $*"
}

fail() {
  echo "[bootstrap] ERROR: $*" >&2
  exit 1
}

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${ENV_TEMPLATE}" "${ENV_FILE}"
  log "created ${ENV_FILE} from template"
fi

if [[ ! -f "${STRATEGY_FILE}" ]]; then
  cp "${STRATEGY_TEMPLATE}" "${STRATEGY_FILE}"
  log "created ${STRATEGY_FILE} from template"
fi

if [[ -z "${UV_BIN}" ]]; then
  log "uv not found; skipped dependency sync"
  log "bootstrap completed with config files only"
  exit 0
fi

"${UV_BIN}" sync --directory "${APP_DIR}"
log "bootstrap completed"
