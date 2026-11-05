# 设计：市场驱动的异构 Agent 实例部署（服务 / 定时任务）

- 状态：提案（Draft）；尚未实现，不代表已交付能力
- 日期：2026-11-04
- 范围：从社区市场（YeYing Node Registry）部署 Codex / Claude Code / OpenClaw 等异构 Agent 实例，支持"常驻服务"与"定时任务"两种部署形态，并在 Orchestrator 控制面统一管理
- 关联：[产品定位与领域架构](product-architecture.md)、[Hub 架构 ADR](hub-architecture-adr.md)、[Agent Runtime Protocol](agent-runtime-design.md)、[运行时数据规划](runtime-data.md)

## 1. 背景与动机

现有仓库已经把"异构 Agent Runtime 控制面"写成北极星目标，并点名 Codex CLI、Claude Code CLI、OpenClaw。但当前实现只覆盖了目标的一小部分，且存在两条**互不相通的执行链**：

- **链 A（市场应用）**：`Project → Control Plane → lifecycle_worker → Node Registry 签名制品 → 受限 Docker Compose`。只会部署 Compose 服务，不理解"codex/claude 这类 CLI 实例"。
- **链 B（Runtime 命令）**：`Web → Backend → Hub Command → OpenClaw Adapter → 本机进程`。只有 OpenClaw 一个真实 Adapter。

两条链在现有文档中被刻意分开（见 `agent-runtime-design.md` 开头）。本设计的目标是在**不破坏这条安全边界**的前提下，把用户视角的一句话需求落地：

> 从市场选一个 Codex / Claude / OpenClaw 制品 → 部署成**服务**或**定时任务** → 在 Orchestrator 中心统一管理（期望/观测状态、日志、审计、重试）。

### 现状与差距对照

| 用户设想 | 仓库现状 | 差距 |
|---|---|---|
| 结合 marketplace 部署 | `node_registry.py`：Ed25519 验签、checksum、release digest、受限 Compose、单节点升级回滚 | 市场只产出 Docker Compose 应用，不产出 Runtime 实例 |
| Codex / Claude / OpenClaw 异构实例 | 仅 OpenClaw 有真 Hub Adapter；`adapters/factory.py` 只有 `trader`/`messenger`（业务应用工作台，非 runtime adapter） | Codex / Claude Adapter 完全未动（ADR 阶段 4） |
| 部署成**服务** | OpenClaw 常驻进程；Compose loopback service | 单机、静态端口、无多租户路由 |
| 部署成**定时任务** | 不存在（全仓库无 cron/schedule 概念） | 整个"定时"维度空白 |
| 在 Orchestrator 管理 | 控制面 + Web + PostgreSQL 持久化 | 单机、无组织/RBAC、Hub 无守护进程安装 |

## 2. 目标与非目标

### 目标

1. 把"部署形态"（`service` / `scheduled`）建模为 Runtime Instance 的一等属性，贯通期望状态、命令协议与 Web。
2. 让 Hub Adapter 契约支持"一次性运行并退出"（run-once）语义，并由 Hub 内置受 lease 保护的调度器按计划触发。
3. 按真实 CLI 接口接入 Codex / Claude Code Adapter，只把三者**真正共有**的字段放进公共模型。
4. 让 Node Registry 制品声明 `runtime_type` 与默认部署形态，使 lifecycle_worker 能把市场制品转成对应 Hub Adapter 的 typed 命令——两条执行链在此收敛。

### 非目标（本设计明确不做）

- 不做组织级 RBAC / 多租户管理后台（属 ADR 阶段 5，与本能力线正交）。
- 不做 Hub 守护进程安装/升级与远程多机生产验收（阶段 5）。
- 不引入通用 shell 执行后门：所有新增命令仍是 typed payload，不接受任意 shell 字段。
- 不把 Backend 变成直接 SSH/入站连接 Hub 的架构（沿用 ADR：Hub 主动出站）。
- 不承诺跨机器调度：调度触发由各 Hub 本地完成，Backend 只下发期望的调度定义。

## 3. 领域模型变更

在现有对象关系（见 product-architecture.md）基础上，对 **Runtime Instance** 增加部署形态维度：

```text
Runtime Instance
├── deployment_mode: service | scheduled      # 新增
├── schedule: { kind: cron|interval, expr }   # 新增，仅 scheduled 时必填
├── desired_state: running | stopped          # service 语义；scheduled 时表示"调度是否启用"
└── runs[]: ScheduledRun                       # 新增，scheduled 的每次执行是一条可观测记录
```

### 3.1 部署形态语义

- **service（常驻服务）**：沿用现有语义。`desired_state=running` 表示应保持常驻；`observed_state` 由 Hub 健康证据决定。OpenClaw 现状即此形态。
- **scheduled（定时任务）**：`desired_state=running` 表示"调度启用"，`stopped` 表示"暂停调度"。真正的执行是**离散的一次性运行（run-once）**，每次运行产出一条 `ScheduledRun`，有独立的 `queued → running → succeeded/failed/cancelled` 终态与退出码、日志引用、耗时。

### 3.2 ScheduledRun（新对象）

```text
ScheduledRun
├── run_id
├── instance_id
├── trigger: schedule | manual        # 定时触发 or 手动"立即运行一次"
├── scheduled_for / started_at / finished_at
├── state: queued|running|succeeded|failed|cancelled|unknown
├── exit_code
├── log_ref
└── command_id                        # 对应下发的 Hub run-once 命令
```

这是对 Temporal 式"可恢复任务"的借鉴（见 product-architecture 市场参考），但保持单节点首期边界：持久化、幂等、lease、重试，不假设跨进程 exactly-once。

## 4. 协议变更（Hub Protocol v1，additive 修订）

现有 `protocols/hub_v1.py` 的 `HubCommandOperation` 全是服务型实例生命周期，`RuntimeObservedState` 也没有"任务完成"终态。需要**向后兼容地扩展**（修改 wire model 后按 ADR 要求重新 `export_hub_schema` 并更新契约测试）。

> 实现决定：`protocol_version` **保持 `1.0`**，不升 1.1。新增内容全部是 additive（新枚举值、新 payload/result union 成员、新事件类型），且 Backend 只向在 heartbeat/inventory 中申报了对应 capability 的 Hub 下发 `run_instance_once`/`cancel_run`（`hubs.py` 既有能力闸门）；旧 Hub 不申报就不会收到，旧 Backend 收到未知 `scheduled_run` 事件也只是原样持久化。升版本号会迫使所有客户端同步升级，却换不来额外安全性。

### 4.1 新增命令操作

```python
class HubCommandOperation(StrEnum):
    ...                       # 现有 10 个保持不变
    RUN_INSTANCE_ONCE = "run_instance_once"   # 新增：执行一次并退出
    CANCEL_RUN = "cancel_run"                 # 新增：协作取消进行中的 run
```

- `run_instance_once` 的 payload 为 runtime-specific typed union（与现有 OpenClaw payload 同样风格），携带本次运行的输入（如 Codex/Claude 的 prompt/任务引用、工作区引用），**不含 secret 明文**，只含受授权的 secret 引用。
- 结果为 typed `instance_run_result`：`exit_code`、`log_ref`、`started_at/finished_at`、脱敏摘要。
- 取消语义复用 ADR 已定义的 command lease + 协作取消：真实 Adapter 必须把取消映射为有界的子进程中断与清理；不能确认已停止时报告 `unknown`。

### 4.2 新增观测状态 / 调度能力

- `RuntimeObservedState` 增加 `COMPLETED`（一次性运行成功退出的瞬时/归档态）。注意：scheduled 实例的"实例级" observed_state 表示调度器是否就绪，单次结果落在 `ScheduledRun`。
- `HubRuntimeInstanceV1` 增加可选 `deployment_mode` 与 `schedule` 回报字段，便于 inventory 对齐期望。
- Adapter 能力目录（`capabilities`）新增 token，例如 `run-once`、`schedule-local`；不声明该能力的 runtime，Backend 不允许以 scheduled 形态创建。

### 4.3 调度触发归属

调度**不在 Backend 定时轮询下发命令**，而是：Backend 下发/更新 `schedule` 作为期望状态；Hub 内置一个受本地状态库 lease 保护的调度器，按 `schedule` 到点生成 `run_instance_once` 的本地命令并执行，运行结果作为事件回报 Backend。这样离线也能按本地时钟触发，重连后补报 run 事件，符合 ADR 的"Hub 负责本机执行、Backend 负责期望与审计"。

> 幂等关键：每个 scheduled run 以 `instance_id + scheduled_for` 作为幂等键，避免 Hub 重启后对同一时刻重复触发。

## 5. 分阶段实施

依赖顺序 **A → B → C → D**，每阶段都保留 API、协议契约测试与安全负向测试（沿用 ADR 要求）。

### 阶段 A — 部署形态建模（纯 Backend，改动可控）

- 数据库：`AgentInstance` 增加 `deployment_mode`、`schedule`；新增 `scheduled_runs` 表（含 lease/幂等/retry 字段，复用 lifecycle task 的持久化模式）。Alembic 迁移。
- Schema：`RuntimeInstanceView` / `RuntimeInstanceCreateRequest` 增加形态与调度字段并做校验（cron 表达式合法性、scheduled 必须声明 schedule）。
- 协议：`hub_v1` 增加回报字段（4.2），导出 JSON Schema，更新契约测试。
- Web：创建实例时选择"服务 / 定时任务"，定时填写计划；实例列表区分两种形态。
- **本阶段不改 Hub 执行逻辑**，scheduled 实例可先只落期望状态、不真正触发（显式标注"调度未执行"）。
- 实现进度：A1 数据库模型与迁移 `0010_instance_deployment_mode`、A2 Pydantic schema 校验、A3 `croniter` 依赖、A4 协议回报字段（`HubRuntimeInstanceV1.deployment_mode/schedule`，`protocol_version` 保持 `1.0`）、A5 服务层贯通（路由 → `HubOpenClawCreatePayloadV1` → Backend 目录行 → Hub Adapter → 本机状态 → inventory 回报 → 目录镜像）、A6 诚实护栏（Backend 对 scheduled 实例的 start/stop 返回 409；Hub inventory 不为 scheduled 实例申报 `start/stop/restart_instance` 能力；`OpenClawRuntimeService.start_instance` 显式拒绝）已完成并有测试（`backend/tests/test_instance_deployment_mode.py`、`test_public_auth_api.py::test_runtime_console_creates_scheduled_instance_and_refuses_lifecycle_actions`）；A7 Web 已完成（Messenger 创建表单增加"部署形态"与 cron / 间隔计划输入并附阶段说明；运行时页与 Messenger 列表/详情对 scheduled 实例显示"定时任务 / 调度未执行"徽标、隐藏启动 / 停止、展示计划与空态运行历史；`npm run lint` 与 `npm run build` 通过，未做浏览器人工核验）。阶段 A 至此完整交付。

### 阶段 B — 定时执行能力（Hub + 命令闭环）

- `hub_v1` 增加 `run_instance_once` / `cancel_run` 操作与 typed 结果（4.1）。
- Hub 客户端增加本地调度器：读取已授权 scheduled 实例，按本地时钟到点生成 run-once 命令，`instance_id+scheduled_for` 幂等，执行走现有 inbox/outbox/lease/取消通道。
- Backend 持久化 `ScheduledRun`：queued→running→终态，退避重试到最大次数，超时清理。
- Web：展示运行历史、单次日志、"立即运行一次"手动触发、暂停/恢复调度。
- 先用现有 OpenClaw Adapter 验证 run-once（例如一次性诊断/脚本），再进入阶段 C 的真实 CLI。
- 实现进度：**B1 协议契约已完成**——`run_instance_once` / `cancel_run` 操作，typed `HubRunOncePayloadV1`（不含任何命令行/参数/环境字段，运行内容由 Hub 上的实例定义决定）与 `HubCancelRunPayloadV1`，typed 结果 `HubRunOnceResultV1`（含时间线/退出码一致性校验、相对 `logs/runs/*` 日志引用、≤2 KiB 脱敏摘要）与 `HubRunCancelledResultV1`，`RuntimeObservedState.COMPLETED`，以及 Hub 本地触发上报用的 `HubScheduledRunEventV1`（`(instance_id, scheduled_for)` 为幂等键，manual 必须关联 Backend command）。两个命令模型的 payload 规则抽成共享 `validate_command_payloads`。契约测试见 `backend/tests/test_instance_deployment_mode.py::RunOnceProtocolContractTest`。
- 实现进度：**B2 Hub 本地调度器已完成**——`hub_client/scheduler.py::latest_slot` 用 `croniter` 计算 `(anchor, now]` 内最近一个时隙（**最多返回一个**：Hub 离线跨越多个时隙只补最近一次，绝不回放）；Hub SQLite store 新增 `hub_client_runs` 表，`(instance_id, scheduled_for)` 唯一约束即幂等键，`schedule_anchor` 以"最近一次已触发时隙"或"本 Hub 首次观察到该计划的时刻"为锚；客户端每轮最多触发一次（串行，不拖慢 Backend 轮询），本地触发**不经 Backend 命令/lease 机制**，由独立的超时 + 关停取消约束，仅通过 `scheduled_run` 事件上报；Backend 发起的手动 `run_instance_once` 命令同样记录为 run 并发同一事件流（带 `command_id`）；进程崩溃残留的 queued/running 运行在重启时报 `unknown / RUN_INTERRUPTED`，不重跑。**计划默认不启用**：`schedule_enabled` 等价于实例 `desired_state == running`，新建的定时实例必须显式启用才会触发（B3 把 scheduled 实例的 start/stop 映射为启用/停用）。OpenClaw 的 run-once 工作负载在本阶段是一次有界、无副作用的诊断探针（写 `logs/runs/<run_id>.log`，实例状态置 `completed`），**不是 Agent 任务**，真实 CLI 工作负载属阶段 C。测试见 `backend/tests/test_hub_scheduler.py`。
- 实现进度：**B3 Backend 已完成**——`hubs.py::accept_events` 对 `scheduled_run` 事件 upsert `ScheduledRun`（按 `run_id` 或 `(instance_id, scheduled_for)` 匹配，`RUN_STATE_ORDER` 保证迟到/重放的早期阶段不会回退终态；目录中不存在的实例只保留原始事件、不归属）；scheduled 实例的 `start`/`stop` 映射为**启用/停用计划**（`OpenClawRuntimeService.start_instance/stop_instance` 只改 `desired_state`，不拉起/杀死 gateway；Hub Adapter 不再剥离这两个能力，A6 的 Backend 409 守卫撤销）；新增 `POST /runtime/instances/{id}/actions/run`（仅 scheduled，下发 `run_instance_once` manual 命令，响应含 `run_id`）与 `GET /runtime/instances/{id}/runs`（按 `scheduled_for` 倒序，owner 校验）；`RuntimeInstanceView.schedule_executed` 改为数据驱动并新增 `latest_run_state/latest_run_at`。修复了一个真实 bug：`claim_commands` 重建命令时只给三个 OpenClaw 操作挂 `operation_payload`，导致 `run_instance_once` 在被 Hub 领取时 payload 丢失、校验失败——改为"非 create 且有存储 payload 即挂载"。端到端测试 `test_public_auth_api.py::test_runtime_console_schedules_runs_through_hub` 用真实 Hub 客户端 + 假时钟覆盖未启用不触发、启用、手动运行、本地触发落库、停用、常驻实例拒绝 run。
- 实现进度：**B4 Web 已完成，阶段 B 至此完整交付**——运行时页对 scheduled 实例提供"启用 / 停用"（`start`/`stop`）与"运行一次"（`POST actions/run`），"期望 / 实际"列改为"计划 · 已启用/未启用 + 上次运行结果"徽标（由 `schedule_executed`/`latest_run_state` 驱动，取代硬编码的"调度未执行"），详情区的运行历史接 `GET runs`（触发时间、来源、结果、退出码、耗时、日志引用，自动刷新），空态文案如实说明 OpenClaw 单次运行目前是诊断探针；Messenger 详情恢复启用/停用并增加"运行一次"与跳转运行历史；TS 类型新增 `RunState`、`ScheduledRun(ListResponse)`、`RuntimeInstanceActionResponse.run_id`、`RuntimeInstance.latest_run_state/latest_run_at`、`BotInstanceView.desired_state`。`npm run lint` 与 `npm run build` 通过（typed router 捕获了一处错误路由并已修正），未做浏览器人工核验。

### 阶段 C — Codex / Claude Code Adapter

- 按真实 CLI 接口分别验证：安装探测、workspace 隔离、会话/一次性任务、模型供应商凭证、日志脱敏、执行取消、离线恢复。
- 只把三者**共有**的字段放进公共模型，其余保留 Adapter 专属扩展字段（ADR Adapter 合约要求）。
- 未实现的能力显式返回 `unsupported`，不做 shell fallback 伪装通用层。
- 定时场景（"每天跑一次 codex 任务"）是 CLI 类 runtime 最自然的用法，与阶段 B 天然契合，优先覆盖。
- 模型供应商（OpenAI/Anthropic 凭证）单独建模，不等同于 runtime_type。
- 实现进度：**C1 Hub 侧 Adapter 已完成，并已对真实 CLI 验证**——协议新增 `HubCliTaskCreatePayloadV1`（`kind=cli_task`：prompt 是实例定义而非每次运行的输入；Codex 沙箱只允许 `read-only|workspace-write`，Claude 权限模式只允许 `plan|acceptEdits|dontAsk`，两个 CLI 的 bypass 模式在协议上**不可表达**；CLI 任务没有常驻形态，恒为 scheduled），`create_payload` 成为按 `kind` 区分的 union，共享校验器按 runtime 匹配 payload。`hub_client/cli_adapter.py` 的 `CliTaskAdapter` 基类（Hub 本地 JSON 状态、每实例 workspace、独立进程组 + 协作取消 + 每实例超时、环境变量密钥脱敏、`logs/runs/<run_id>.log`）与 `CodexAdapter`（`codex exec --json -o … --skip-git-repo-check --ephemeral -C <workspace> -s <sandbox>`）、`ClaudeCodeAdapter`（`claude --print --output-format json --permission-mode <mode> --no-session-persistence -p <prompt>`）；argv 形态来自仓库 `agents/openclaw/coder` 已验证的调用，**去掉了**其默认的 `--dangerously-bypass-approvals-and-sandbox`。未安装的 CLI 申报空能力集（Backend 闸门自然拒绝），`get_logs/diagnose/update_model` 对 CLI 运行时不申报。Hub CLI 新增 `--codex` / `--claude-code` 与 `HUB_CODEX_BIN` / `HUB_CLAUDE_CODE_BIN`。测试 `backend/tests/test_cli_adapters.py` 用会记录 argv 的假 `codex`/`claude` 可执行文件证明传了哪些、没传哪些参数，并覆盖失败、超时、取消、启用/停用/删除、恢复语义与 Hub 本地调度集成。**真实验证**（2026-11-05，本机 `codex-cli 0.166.0`、`Claude Code 2.1.318`，使用机器已有登录）：两者均成功执行最小 prompt 并返回 `OK`，Codex 7.1s / Claude 3.4s，退出码 0。尚未做：模型供应商凭证经 secret 引用注入（目前继承 Hub 进程环境、使用机器已有登录）、C2 Backend 目录泛化（创建路由仍只接受 OpenClaw，实例目录经 `OpenClawRuntimeService` 读取）与 Web。

### 阶段 D — Marketplace → Runtime 桥接（两链收敛）

- Node Registry 制品的 `runtime.json` 扩展：声明 `runtime_type`（codex/claude/openclaw）与 `default_deployment_mode`、可选默认 `schedule`。保持 Ed25519 验签与受信任发布者 key 治理不变。
- `lifecycle_worker` 分流：当制品是 Compose 应用时沿用现有受限 Compose 部署；当制品声明为 runtime 实例时，转换为对应 Hub Adapter 的 typed `create_instance`（并按形态附带 schedule）。
- 统一任务与审计：区分"Node Registry 社区应用生命周期任务"与"Runtime 命令"，再统一幂等、超时、重试、回滚、审计、告警策略（对应 product-architecture 重构阶段 4）。
- 市场安装请求仍需指定已发布 SemVer（Node API 不支持 `latest`）。

## 6. 数据与状态边界

- PostgreSQL：实例形态、schedule、ScheduledRun、命令、事件、审计元数据（权威状态）。
- Hub 本地 SQLite：inbox/outbox、事件 cursor、调度器触发记录（本机执行真相与离线缓存）。
- Secret：仍由本机 secret store 解析受授权引用；run-once payload 不含明文；日志/事件/错误过滤凭证。
- Warehouse / 向量库：仅异步归档与可重建的派生索引，不作权威状态源（沿用 runtime-data.md）。

## 7. 安全与权限

- 新增命令仍是有 schema + 权限检查的 typed 操作，不接受任意参数对象或 shell 字段。
- 破坏性操作、工作目录范围、执行审批由 Backend 授权策略与 Hub 本地策略共同约束，任一拒绝都不执行。
- 调度定义变更、每次 run 触发、取消、结果均关联审计主体与 trace ID。
- scheduled 实例的"立即运行一次"与"暂停调度"属受审计的用户动作。

## 8. 验收边界（在实现并验收前不得宣称）

- 在阶段 C 完成前，不宣称"支持任意 runtime 纳管"；Codex/Claude 能力须逐项真实验证。
- 在阶段 D 完成前，不宣称市场可一键部署异构 runtime 实例。
- 单节点首期：静态端口、单机执行；多 Project 端口预留、租户路由、公网反向代理、RBAC、多机验收仍是上线前事项（阶段 5，本设计不覆盖）。
- 定时调度为 Hub 本地触发；不承诺跨机器集中调度或跨进程 exactly-once。

## 9. 阶段依赖图

```text
A（部署形态建模，纯 Backend）
    └── B（run-once + 本地调度器，填补"定时"空白）
            └── C（Codex/Claude Adapter，异构从 1 → 3）
                    └── D（市场制品声明 runtime_type，两链收敛）
                            └── [阶段 5：RBAC / Hub 守护进程 / 多机生产，另行治理]
```

A/B 代价最小且解锁缺失最多的"定时"维度；C 扩展异构；D 统一市场入口。生产化（阶段 5）与本能力线正交，按需并行。
