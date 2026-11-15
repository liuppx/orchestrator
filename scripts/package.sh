#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/package-common.sh"
package_common_init orchestrator "$@"
for command_name in npm uv; do command -v "$command_name" >/dev/null 2>&1 || package_fail "$command_name is required"; done
[[ -f "$PACKAGE_ROOT_DIR/backend/uv.lock" ]] || package_fail 'backend/uv.lock is required'
(cd "$PACKAGE_ROOT_DIR/web" && npm install --no-audit --no-fund && npm run build)
(cd "$PACKAGE_ROOT_DIR/backend" && uv sync --locked --no-dev)
[[ -f "$PACKAGE_ROOT_DIR/web/dist/index.html" ]] || package_fail 'web build output is missing'
package_make_root orchestrator-service
for directory in backend agents; do [[ ! -d "$PACKAGE_ROOT_DIR/$directory" ]] || cp -R "$PACKAGE_ROOT_DIR/$directory" "$PACKAGE_DIR/$directory"; done
if [[ -d "$PACKAGE_DIR/agents" ]]; then
  find "$PACKAGE_DIR/agents" -type d \( -name .venv -o -name node_modules -o -name __pycache__ \) -prune -exec rm -rf {} +
fi
venv_python="$PACKAGE_DIR/backend/.venv/bin/python"
if [[ -L "$venv_python" ]]; then
  resolved_python="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$venv_python")"
  [[ -x "$resolved_python" ]] || package_fail "virtualenv interpreter target is missing: $resolved_python"
  cp -p "$resolved_python" "$venv_python.tmp"
  mv "$venv_python.tmp" "$venv_python"
fi
if [[ -d "$PACKAGE_ROOT_DIR/tests/smoke" ]]; then
  mkdir -p "$PACKAGE_DIR/tests"
  cp -R "$PACKAGE_ROOT_DIR/tests/smoke" "$PACKAGE_DIR/tests/smoke"
fi
for file in .env.template backend/.env.template; do
  [[ ! -f "$PACKAGE_ROOT_DIR/$file" ]] || { mkdir -p "$PACKAGE_DIR/$(dirname "$file")"; cp "$PACKAGE_ROOT_DIR/$file" "$PACKAGE_DIR/$file"; }
done
package_copy_scripts "$PACKAGE_DIR/scripts"
package_strip_build_files "$PACKAGE_DIR"
package_finish_artifact service orchestrator

package_make_root orchestrator-web
PACKAGE_HEALTH_PATH=/index.html
cp -R "$PACKAGE_ROOT_DIR/web/dist/." "$PACKAGE_DIR/"
package_finish_artifact static
package_finish
