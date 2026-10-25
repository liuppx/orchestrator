#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

CURRENT_PORT="$(gateway_port)"
pids="$(find_running_pids || true)"

if [[ -z "${pids}" && ! -f "${PID_FILE}" ]]; then
  if is_port_ready "${CURRENT_PORT}"; then
    echo "port ${CURRENT_PORT} is listening, but openclaw pid is not visible from this shell"
    echo "try from the host shell: fuser -k ${CURRENT_PORT}/tcp"
  else
    echo "not running"
  fi
  exit 0
fi

for p in ${pids}; do
  kill "${p}" 2>/dev/null || true
done
sleep 2
for p in ${pids}; do
  kill -9 "${p}" 2>/dev/null || true
done

for gp in $(ss -lntp 2>/dev/null | awk -v p=":${CURRENT_PORT}" '$4 ~ p && /openclaw-gatewa/ {gsub(/.*pid=/,"",$NF); gsub(/,.*/,"",$NF); print $NF}'); do
  kill "${gp}" 2>/dev/null || true
  kill -9 "${gp}" 2>/dev/null || true
done

rm -f "${PID_FILE}"
if is_port_ready "${CURRENT_PORT}"; then
  echo "stop requested, but port ${CURRENT_PORT} is still listening"
  echo "try from the host shell: fuser -k ${CURRENT_PORT}/tcp"
else
  echo "stopped"
fi
