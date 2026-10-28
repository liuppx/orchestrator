# 跨境电商自开发维护军队（OpenClaw）

## 目标
- 让 WhatsApp 电商智能体具备“自动巡检、自动拉起、自动产出改进建议”的能力。
- 采用“生产稳定优先 + 只读自开发建议 + 人工闸门发布”的安全策略。

## 组件
- `bin/watchdog.sh`：每分钟巡检，必要时自动拉起 gateway。
- `bin/worker_observe.sh`：生成运行态观察报告。
- `bin/worker_verify.sh`：执行健康检查并产出验收报告。
- `bin/worker_codex_advisor.sh`：调用 Codex（只读）生成改进建议。
- `bin/worker_codex_swarm.sh`：多角色 Codex 并行建议（可选，成本更高）。
- `bin/scheduler.sh`：常驻调度器。
- `bin/start_army.sh` / `bin/stop_army.sh` / `bin/status_army.sh`：运维入口。

## 快速启动
```bash
cd /home/administrator/bot
cp -n ops/army/army.env.template ops/army/army.env
bash ops/army/bin/start_army.sh
bash ops/army/bin/status_army.sh
```

## 输出目录
- 报告：`/home/administrator/bot/ops/reports`
- 建议：`/home/administrator/bot/ops/reports/advisor`
- 日志：`/home/administrator/bot/runtime/logs`
- 升级清单：`/home/administrator/bot/runtime/escalation/pending_questions.md`

## 安全边界
- 生产默认只做自愈和建议，不自动改业务代码。
- Codex 工作者默认 `read-only`，输出建议而非直接改仓库。
- 真正变更仍走人工确认（PR/手动合并）。
