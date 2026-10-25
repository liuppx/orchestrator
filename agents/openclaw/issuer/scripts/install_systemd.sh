#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

SERVICE_NAME="${SERVICE_NAME:-issuer-openclaw-gateway}"
BOT_USER="${BOT_USER:-${SUDO_USER:-$(id -un)}}"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

ensure_layout
[[ -f "${OPENCLAW_CONFIG_PATH}" ]] || fail "OpenClaw config not found: ${OPENCLAW_CONFIG_PATH}"

TMP_SERVICE="$(mktemp)"

cat >"${TMP_SERVICE}" <<EOF
[Unit]
Description=Issuer OpenClaw Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
WorkingDirectory=${APP_DIR}
Environment=APP_DIR=${APP_DIR}
Environment=GITHUB_ENV_FILE=${GITHUB_ENV_FILE}
Environment=OPENCLAW_CONFIG_PATH=${OPENCLAW_CONFIG_PATH}
Environment=OPENCLAW_STATE_DIR=${STATE_DIR}
Environment=LOG_PATH=${LOG_PATH}
Environment=PID_FILE=${PID_FILE}
ExecStart=/bin/bash ${APP_DIR}/scripts/run_gateway.sh
Restart=always
RestartSec=3
StandardOutput=append:${LOG_PATH}
StandardError=append:${LOG_PATH}

[Install]
WantedBy=multi-user.target
EOF

"${SUDO[@]}" install -m 0644 "${TMP_SERVICE}" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "${TMP_SERVICE}"

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable "${SERVICE_NAME}"
"${SUDO[@]}" systemctl restart "${SERVICE_NAME}"
"${SUDO[@]}" systemctl status --no-pager "${SERVICE_NAME}"
