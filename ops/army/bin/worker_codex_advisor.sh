#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

if [[ "${ARMY_ENABLE_CODEX}" != "1" ]]; then
  printf '[%s] codex advisor disabled\n' "$(timestamp)" | tee -a "${ADVISOR_LOG}"
  exit 0
fi

if ! command -v "${CODEX_BIN}" >/dev/null 2>&1; then
  append_pending "advisor" "codex_missing" "install_codex_cli"
  exit 1
fi

latest_report="$(ls -1t "${REPORT_DIR}"/observe-*.md "${REPORT_DIR}"/verify-*.md 2>/dev/null | head -n 1 || true)"
if [[ -z "${latest_report}" ]]; then
  "${SCRIPT_DIR}/worker_observe.sh" || true
  latest_report="$(ls -1t "${REPORT_DIR}"/observe-*.md 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${latest_report}" ]]; then
  append_pending "advisor" "no_report_available" "run_worker_observe"
  exit 1
fi

ts="$(date '+%Y%m%d-%H%M%S')"
advice_file="${ADVISOR_DIR}/advice-${ts}.md"
prompt_file="${ARMY_STATE_DIR}/advisor_prompt_${ts}.txt"
run_log="${ADVISOR_DIR}/advisor-run-${ts}.log"

cat > "${prompt_file}" <<PROMPT
你是“跨境电商 WhatsApp 智能体”运维与产品联席指挥官。
请基于以下运行报告给出改进建议，不要执行任何命令，不要修改任何文件：
报告路径：${latest_report}

输出要求（中文Markdown）：
1) 先给 P0 / P1 / P2 三层优先级动作；
2) 每个动作给“目的、预期收益、执行命令（如有）、风险”；
3) 专注跨境电商对话场景（价格、MOQ、交期、升级老板）；
4) 严禁输出任何密钥或敏感信息；
5) 给出“下一次巡检关注点”清单。
PROMPT

if run_with_timeout "${ARMY_CODEX_TIMEOUT_SEC}" "${CODEX_BIN}" exec -C "${BOT_ROOT}" -s "${ARMY_CODEX_SANDBOX}" --skip-git-repo-check -o "${advice_file}" - < "${prompt_file}" > "${run_log}" 2>&1; then
  cp "${advice_file}" "${ADVISOR_DIR}/latest-advice.md"
  printf '[%s] advisor wrote %s\n' "$(timestamp)" "${advice_file}" | tee -a "${ADVISOR_LOG}"
else
  append_pending "advisor" "codex_advisor_failed" "inspect_${run_log##*/}"
  printf '[%s] advisor failed, see %s\n' "$(timestamp)" "${run_log}" | tee -a "${ADVISOR_LOG}"
  exit 1
fi
