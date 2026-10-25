#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/config/hub.env"
APP_ENV_FILE="$ROOT_DIR/backend/.env"

echo "[doctor] repo=$ROOT_DIR"

for cmd in bash curl jq python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "[ok] $cmd"
  else
    echo "[warn] missing command: $cmd"
  fi
done

if command -v uv >/dev/null 2>&1; then
  echo "[ok] uv $(uv --version)"
else
  echo "[warn] uv missing"
fi

if [[ -x "$ROOT_DIR/backend/.venv/bin/agent-hub" ]]; then
  echo "[ok] python hub executable ready"
else
  echo "[warn] python hub executable missing; run: (cd backend && uv sync)"
fi

if command -v openclaw >/dev/null 2>&1; then
  echo "[ok] openclaw $(openclaw --version 2>/dev/null || true)"
else
  echo "[warn] openclaw missing"
fi

ACTIVE_ENV=""
if [[ -f "$ENV_FILE" ]]; then
  ACTIVE_ENV="$ENV_FILE"
elif [[ -f "$APP_ENV_FILE" ]]; then
  ACTIVE_ENV="$APP_ENV_FILE"
fi

if [[ -n "$ACTIVE_ENV" ]]; then
  echo "[ok] env file exists: $ACTIVE_ENV"
  # shellcheck disable=SC1091
  source "$ACTIVE_ENV" || true
  if [[ -n "${ROUTER_API_KEY:-}" ]]; then
    echo "[ok] ROUTER_API_KEY configured"
    ROUTER_MODELS_URL="${ROUTER_BASE_URL:-https://test-router.yeying.pub/v1}/models"
    if curl -fsS "$ROUTER_MODELS_URL" -H "Authorization: Bearer $ROUTER_API_KEY" -o /tmp/hub_router_models.json; then
      echo "[ok] router endpoint reachable"
      rm -f /tmp/hub_router_models.json
    else
      echo "[warn] router endpoint request failed: $ROUTER_MODELS_URL"
    fi
  else
    echo "[warn] ROUTER_API_KEY is empty in $ACTIVE_ENV"
  fi
else
  echo "[warn] env file missing: $ENV_FILE"
fi

echo "[next] run:       bash $ROOT_DIR/scripts/starter.sh start"
