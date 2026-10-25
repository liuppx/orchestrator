#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

TARGET_WORKSPACE="${1:-${WORKSPACE_DIR}}"
RUNTIME_POLICY_PATH="${TARGET_WORKSPACE}/config/policy.json"
SOURCE_POLICY_PATH="${APP_POLICY_FILE}"

mkdir -p "${TARGET_WORKSPACE}/tools/lib"
mkdir -p "${TARGET_WORKSPACE}/skills"
mkdir -p "${TARGET_WORKSPACE}/hooks"
mkdir -p "${TARGET_WORKSPACE}/config"
mkdir -p "${TARGET_WORKSPACE}/state/pending-actions"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "${APP_DIR}/workspace_assets/tools/" "${TARGET_WORKSPACE}/tools/"
  rsync -a --delete "${APP_DIR}/workspace_assets/skills/" "${TARGET_WORKSPACE}/skills/"
  rsync -a --delete "${APP_DIR}/workspace_assets/hooks/" "${TARGET_WORKSPACE}/hooks/"
else
  rm -rf "${TARGET_WORKSPACE}/tools" "${TARGET_WORKSPACE}/skills" "${TARGET_WORKSPACE}/hooks"
  mkdir -p "${TARGET_WORKSPACE}/tools/lib" "${TARGET_WORKSPACE}/skills" "${TARGET_WORKSPACE}/hooks"
  cp -R "${APP_DIR}/workspace_assets/tools/." "${TARGET_WORKSPACE}/tools/"
  cp -R "${APP_DIR}/workspace_assets/skills/." "${TARGET_WORKSPACE}/skills/"
  cp -R "${APP_DIR}/workspace_assets/hooks/." "${TARGET_WORKSPACE}/hooks/"
fi

if [[ -d "${APP_DIR}/workspace_assets/root" ]]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${APP_DIR}/workspace_assets/root/" "${TARGET_WORKSPACE}/"
  else
    cp -R "${APP_DIR}/workspace_assets/root/." "${TARGET_WORKSPACE}/"
  fi
fi

if [[ ! -f "${SOURCE_POLICY_PATH}" && -f "${POLICY_TEMPLATE}" ]]; then
  cp "${POLICY_TEMPLATE}" "${SOURCE_POLICY_PATH}"
fi

if [[ -f "${SOURCE_POLICY_PATH}" ]]; then
  cp "${SOURCE_POLICY_PATH}" "${RUNTIME_POLICY_PATH}"
elif [[ -f "${POLICY_TEMPLATE}" ]]; then
  cp "${POLICY_TEMPLATE}" "${RUNTIME_POLICY_PATH}"
fi

log "synced workspace assets to: ${TARGET_WORKSPACE}"
