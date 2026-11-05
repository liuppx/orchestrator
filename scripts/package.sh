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
package_make_root orchestrator
for directory in backend agents; do [[ ! -d "$PACKAGE_ROOT_DIR/$directory" ]] || cp -R "$PACKAGE_ROOT_DIR/$directory" "$PACKAGE_DIR/$directory"; done
mkdir -p "$PACKAGE_DIR/web"
cp -R "$PACKAGE_ROOT_DIR/web/dist" "$PACKAGE_DIR/web/dist"
if [[ -d "$PACKAGE_ROOT_DIR/tests/smoke" ]]; then
  mkdir -p "$PACKAGE_DIR/tests"
  cp -R "$PACKAGE_ROOT_DIR/tests/smoke" "$PACKAGE_DIR/tests/smoke"
fi
for file in .env.template backend/.env.template; do
  [[ ! -f "$PACKAGE_ROOT_DIR/$file" ]] || { mkdir -p "$PACKAGE_DIR/$(dirname "$file")"; cp "$PACKAGE_ROOT_DIR/$file" "$PACKAGE_DIR/$file"; }
done
package_copy_scripts "$PACKAGE_DIR/scripts"
package_strip_build_files "$PACKAGE_DIR"
package_finish_artifact service-with-assets
package_finish
