#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/hub/backend"
FRONTEND_DIR="$ROOT_DIR/hub/frontend"
CONFIG_DIR="$ROOT_DIR/config"
SERVICE_ENV_TEMPLATE="$CONFIG_DIR/hub.env.template"
SERVICE_ENV_FILE="$CONFIG_DIR/hub.env"
APP_ENV_TEMPLATE="$APP_DIR/.env.template"
APP_ENV_FILE="$APP_DIR/.env"

SKIP_OPENCLAW_INSTALL="${SKIP_OPENCLAW_INSTALL:-0}"

upsert_env_value() {
  local key="$1"
  local value="$2"
  local file="$3"
  local tmp_file

  tmp_file="$(mktemp "${TMPDIR:-/tmp}/hub-env.XXXXXX")"
  if [[ -f "$file" ]] && grep -q "^${key}=" "$file"; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        "${key}="*) printf '%s=%s\n' "$key" "$value" ;;
        *) printf '%s\n' "$line" ;;
      esac
    done < "$file" > "$tmp_file"
    mv "$tmp_file" "$file"
  else
    rm -f "$tmp_file"
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

echo "[step] bootstrap hub"
if [[ -x "$APP_DIR/.venv/bin/agent-hub" ]]; then
  echo "[ok] python hub already prepared: $APP_DIR/.venv/bin/agent-hub"
elif command -v uv >/dev/null 2>&1; then
  echo "[step] install python control plane dependencies"
  (cd "$APP_DIR" && uv sync)
else
  echo "[error] uv not found. Install uv first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if [[ ! -f "$SERVICE_ENV_TEMPLATE" ]]; then
  echo "[error] missing service env template: $SERVICE_ENV_TEMPLATE" >&2
  exit 1
fi

if [[ ! -f "$SERVICE_ENV_FILE" ]]; then
  cp "$SERVICE_ENV_TEMPLATE" "$SERVICE_ENV_FILE"
  echo "[info] created $SERVICE_ENV_FILE from template"
fi

if [[ ! -f "$APP_ENV_TEMPLATE" ]]; then
  echo "[error] missing local backend env template: $APP_ENV_TEMPLATE" >&2
  exit 1
fi

if [[ ! -f "$APP_ENV_FILE" ]]; then
  cp "$APP_ENV_TEMPLATE" "$APP_ENV_FILE"
  echo "[info] created local backend env: $APP_ENV_FILE"
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[error] npm not found. Install Node.js/npm first so Hub frontend can be built." >&2
  exit 1
fi

echo "[step] build hub frontend"
(
  cd "$FRONTEND_DIR"
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
  npm run build
)

if ! command -v openclaw >/dev/null 2>&1; then
  if [[ "$SKIP_OPENCLAW_INSTALL" == "1" ]]; then
    echo "[warn] openclaw missing and SKIP_OPENCLAW_INSTALL=1, skip install"
  else
    echo "[step] install openclaw (node + cli)"
    bash "$ROOT_DIR/scripts/setup/openclaw_prepare.sh" install
  fi
fi

if command -v openclaw >/dev/null 2>&1; then
  echo "[ok] openclaw: $(openclaw --version 2>/dev/null || true)"
else
  echo "[warn] openclaw still missing; WhatsApp/DingTalk instances cannot be started until installed"
fi

if [[ -n "${ROUTER_API_KEY:-}" ]]; then
  upsert_env_value "ROUTER_API_KEY" "$ROUTER_API_KEY" "$SERVICE_ENV_FILE"
  upsert_env_value "ROUTER_API_KEY" "$ROUTER_API_KEY" "$APP_ENV_FILE"
  echo "[ok] injected ROUTER_API_KEY into service/local env files from current shell env"
fi

echo "[next] local backend env:   $APP_ENV_FILE"
echo "[next] service runtime env: $SERVICE_ENV_FILE"
echo "[next] start control plane: bash $ROOT_DIR/scripts/starter.sh start"
