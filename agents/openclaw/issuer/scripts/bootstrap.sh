#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

FORCE="${FORCE:-0}"

ensure_layout

if [[ ! -f "${GITHUB_ENV_FILE}" && -f "${GITHUB_ENV_TEMPLATE}" ]]; then
  cp "${GITHUB_ENV_TEMPLATE}" "${GITHUB_ENV_FILE}"
  log "created ${GITHUB_ENV_FILE} from ${GITHUB_ENV_TEMPLATE}"
fi

if [[ ! -f "${APP_POLICY_FILE}" && -f "${POLICY_TEMPLATE}" ]]; then
  cp "${POLICY_TEMPLATE}" "${APP_POLICY_FILE}"
  log "created ${APP_POLICY_FILE} from ${POLICY_TEMPLATE}"
fi

if [[ ! -f "${OPENCLAW_CONFIG_PATH}" ]]; then
  [[ -f "${OPENCLAW_CONFIG_TEMPLATE}" ]] || fail "missing template config: ${OPENCLAW_CONFIG_TEMPLATE}"
  cp "${OPENCLAW_CONFIG_TEMPLATE}" "${OPENCLAW_CONFIG_PATH}"
  sed -i "s|__WORKSPACE_DIR__|${WORKSPACE_DIR}|g" "${OPENCLAW_CONFIG_PATH}"
  log "created ${OPENCLAW_CONFIG_PATH} from ${OPENCLAW_CONFIG_TEMPLATE}"
elif [[ "${FORCE}" == "1" ]]; then
  cp "${OPENCLAW_CONFIG_TEMPLATE}" "${OPENCLAW_CONFIG_PATH}"
  sed -i "s|__WORKSPACE_DIR__|${WORKSPACE_DIR}|g" "${OPENCLAW_CONFIG_PATH}"
  log "recreated ${OPENCLAW_CONFIG_PATH} from template because FORCE=1"
fi

bash "${SCRIPT_DIR}/sync_workspace.sh" "${WORKSPACE_DIR}"

touch "${LOG_PATH}"

log "bootstrap complete"
log "next:"
log "  1. edit ${GITHUB_ENV_FILE}"
log "  2. put GitHub App private key under ${SECRETS_DIR}"
log "  3. edit ${OPENCLAW_CONFIG_PATH}"
log "  4. edit ${APP_POLICY_FILE}"
log "  5. start with: bash ${SCRIPT_DIR}/start_gateway.sh"
