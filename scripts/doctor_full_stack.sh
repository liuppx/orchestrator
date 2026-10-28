#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/backend/.env"

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

if [[ -x "$ROOT_DIR/backend/.venv/bin/orchestrator" ]]; then
  echo "[ok] Orchestrator command ready"
else
  echo "[warn] Orchestrator command missing; run: (cd backend && uv sync)"
fi

if command -v openclaw >/dev/null 2>&1; then
  echo "[ok] openclaw $(openclaw --version 2>/dev/null || true)"
else
  echo "[warn] openclaw missing"
fi

if [[ -f "$ENV_FILE" ]]; then
  echo "[ok] env file exists: $ENV_FILE"
  # shellcheck disable=SC1091
  source "$ENV_FILE" || true
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
    echo "[warn] ROUTER_API_KEY is empty in $ENV_FILE"
  fi
else
  echo "[warn] env file missing: $ENV_FILE (copy backend/.env.template to backend/.env)"
fi

echo "[next] run:       bash $ROOT_DIR/scripts/starter.sh start"
