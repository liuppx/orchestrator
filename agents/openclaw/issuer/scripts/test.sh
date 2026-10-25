#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

node --experimental-strip-types --test --test-concurrency=1 "${APP_DIR}/tests"/*.test.mjs
