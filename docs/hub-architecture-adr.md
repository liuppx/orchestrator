# ADR-001: Backend + Machine Hub 控制架构

- 状态：已采纳；v1 核心消息、服务端 API、本地持久化客户端及 OpenClaw 实例全套 Messenger 操作已接入 Hub 命令；Hub 安装/升级、Runtime 管理 UI 与 RBAC 尚未实现
- 日期：2026-10-31
- 范围：Agent Runtime 的跨机器纳管、指令下发、状态回传

## 背景

Orchestrator 的工作台需要管理运行在不同机器上的 Agent Runtime 实例，例如 Codex CLI、Claude Code CLI、OpenClaw。中心服务如果直接管理本机进程，或者依赖 SSH、浏览器直连机器，就无法形成统一的身份、安全、审计和离线恢复边界。现有 Runtime Node 心跳接口只接收节点信息，尚未构成远程控制闭环；新的 Runtime API 和旧 Messenger `/agent` API 的 OpenClaw 创建、删除、模型配置、启停、日志、诊断及 WhatsApp 配对均委派给 Hub，Backend 只保留 owner/catalog、授权、命令队列和结果镜像。

## 决策

采用中心 Backend + 每台受管机器一个 Hub 的控制面架构：

```text
Web 工作台
    -> Backend / Control Plane
         <- Hub（机器 A，主动建立 HTTPS 请求）
              -> Runtime Adapter -> 本机 Runtime 实例
         <- Hub（机器 B，主动建立 HTTPS 请求）
              -> Runtime Adapter -> 本机 Runtime 实例
```

Hub 主动出站连接 Backend；Backend 不通过 SSH 或公网入站端口连接 Hub。浏览器只访问 Backend。Backend 保存身份、授权、期望状态、命令、观测状态、事件和审计记录；Hub 负责机器本地的实例发现、类型化操作、状态采集和离线缓存。

## 术语与领域边界

- **Backend / Control Plane**：中心工作台和 API，管理用户、组织/项目权限、Hub 注册、实例目录、期望状态、命令队列、观测状态、事件、审计和调度策略。
- **Hub**：部署在每台受管机器上的常驻服务，以单机身份注册，持有本机 Runtime Adapter，主动向 Backend 拉取工作并回报心跳、清单、状态和事件。一个 Hub 对应一台机器；开发机也走相同协议。客户端对本地状态库持有进程锁，避免同一 Hub 重复运行。
- **Runtime Adapter**：Hub 内对某类本机 Runtime 的显式适配器。按能力清单暴露发现、检查、生命周期或执行能力；不支持的能力明确返回 unsupported。
- **Runtime Provider**：现有 API/控制台的能力目录用词，表示可纳管的 Runtime 类型；目标上由各 Hub 上报，具体本机操作由对应 Runtime Adapter 执行。
- **Runtime Instance**：机器上一个可单独识别和观测的 Runtime 实例。Codex、Claude Code 与 OpenClaw 可以有不同的实例、配置和能力模型，不能假设它们具有完全相同的生命周期接口。
- **Workspace / Session / Task**：Runtime 内部的工作目录、会话或一次执行。仅当对应 Adapter 能可靠发现和控制时才纳入统一模型。
- **Model Provider**：OpenAI、Anthropic 等推理服务和凭证配置，与 Runtime Provider 分开建模。Runtime 类型不等同于模型供应商。
- **YeYing Node / Node Registry**：社区应用制品的登记和发布中心，不是运行 Hub 的机器，也不是本 ADR 中的 Hub。

## 职责划分

| Backend | Hub |
|---|---|
| 用户身份、组织/项目和授权 | Hub 身份、机器信息、操作系统与版本 |
| Hub 注册、撤销、能力目录与在线状态 | 本机 Runtime 发现、Adapter 管理与能力上报 |
| 实例目录、期望状态和配置版本元数据 | 本机实例检查、受控操作与健康证据采集 |
| 命令创建、授权、排队、过期和审计 | 命令校验、幂等执行、结果/事件上报 |
| 观测状态、事件时间线、告警和工作台查询 | 离线事件暂存、重连补报、本机 Secret 引用解析 |
| 多机器策略与后续调度 | 不跨机器调度，不决定组织级权限 |

Backend 负责“谁可以让哪台机器上的哪个实例做什么”；Hub 负责“在本机按已声明能力安全地做，并提供可验证结果”。Hub 不应承载组织级权限真相，Backend 也不应直接操作 Hub 的文件系统或本机进程。

## Hub Protocol v1

### 传输

首版采用 Hub 主动发起的 HTTPS 请求和短轮询/长轮询，不要求 Hub 暴露入站端口，不把 WebSocket、gRPC、Kubernetes 或 SSH 作为上线前提。协议使用版本化 JSON API。Pydantic wire contract 位于 `backend/src/orchestrator/protocols/hub_v1.py`，发布的 JSON Schema 位于 `docs/protocols/hub-v1.schema.json`，契约测试位于 `backend/tests/test_hub_protocol_contract.py`。服务端与客户端协议循环已有首期实现；仓库包含内存模拟 Adapter 和真实 OpenClaw 本机 Adapter，支持 create/inspect/start/stop/delete/update-model/logs/diagnose/pair-whatsapp。业务结果使用严格类型化 result union，经本地 outbox、Backend 事件持久化和重放返回；日志结果做脱敏、相对路径化并限制大小，WhatsApp 配对在崩溃恢复时报告 unknown 而不重复启动。

客户端代码位于 `backend/src/orchestrator/hub_client/`：本机 SQLite 保存 inbox、outbox、事件 cursor、Hub heartbeat 和待确认 inventory；命令状态与事件在同一事务提交，网络恢复先补报事件。HTTP transport 对非回环明文 HTTP 拒绝发送凭证。命令恢复时 start/stop 由 Adapter inspect/reconcile，create 先检查本机实例是否已落盘再决定恢复执行。OpenClaw Hub Adapter 使用 Hub 本机 JSON 状态与 Secret Store，不依赖 Backend PostgreSQL；旧数据显式迁移时才临时读取原数据库。Runtime API 与旧 Messenger API 经 Backend 校验 owner 后派发，inventory/事件回填控制面目录。DingTalk secret 仅以目标 Hub 专属 Fernet 密文保存在命令表中，Hub 解密后写本机 Secret Store。`orchestrator-machine-hub --simulate` 仍仅供协议联调；守护进程安装/升级和远程多机生产验收仍未完成，受控 OpenClaw 子进程取消与超时清理已由 Adapter 实现并有回归测试。

修改 wire model 后，在 `backend/` 目录执行 `uv run python -m orchestrator.protocols.export_hub_schema` 更新 JSON Schema；契约测试会检查生成文件与 Python 定义一致。

### 核心交互

1. Hub 通过受控 enrollment 流程取得独立、可撤销和可轮换的 Hub 身份凭证。
2. Hub 上报心跳、Hub 版本、机器元数据、Adapter 能力和 Runtime inventory。
3. Hub 拉取仅授权给该 Hub 的命令；Backend 返回命令 ID、目标实例、操作类型、参数、期望修订号、执行 attempt、trace ID 和过期时间。
4. Hub 先报告 accepted/running 等执行进度，执行期间通过 command lease API 续租，再提交结果、状态快照和事件；Hub 事件带单调递增的本地 sequence/cursor，Backend 按 Hub 去重并持久化后确认游标。
5. Hub 重连时先从最后确认游标补报本地缓冲的结果/事件并刷新 inventory，再领取仍有效的命令、对齐最新 desired revision。

服务端首期路由如下，均在 `/api/v1/internal` 下：

| 方法 | 路径 | 身份 | 行为 |
|---|---|---|---|
| POST | `/hubs/{hub_id}/credentials` | 管理员 `HUB_ADMIN_TOKEN` | 创建 Hub 并签发随机凭证；明文只返回一次，数据库只存 SHA-256 摘要；默认 90 天，可设置 5 分钟至 365 天 |
| GET | `/hubs/{hub_id}/credentials` | 管理员 `HUB_ADMIN_TOKEN` | 查询凭证状态、有效期和最近使用时间，不返回凭证明文 |
| POST | `/hubs/{hub_id}/credentials/{credential_id}/revoke` | 管理员 `HUB_ADMIN_TOKEN` | 撤销凭证；最多允许三个并行有效凭证用于轮换 |
| POST | `/hubs/{hub_id}/heartbeat` | Hub 自身 Bearer 凭证 | 更新机器/Adapter 元数据并返回轮询间隔 |
| PUT | `/hubs/{hub_id}/inventory` | Hub 自身 Bearer 凭证 | 按单调 revision 写入完整 Runtime inventory；相同 revision 只接受相同内容 |
| GET | `/hubs/{hub_id}/commands?limit=10` | Hub 自身 Bearer 凭证 | 领取带 60 秒 lease 的命令，attempt 单调递增；同一实例串行领取；过期 lease 可被新 attempt 回收 |
| POST | `/hubs/{hub_id}/commands/{command_id}/lease` | Hub 自身 Bearer 凭证 | 按 command ID 与 attempt 续租；已过期、被回收或 attempt 不匹配时返回 409 |
| POST | `/hubs/{hub_id}/events` | Hub 自身 Bearer 凭证 | 按连续 sequence 批量写入事件；相同事件重放返回已确认 cursor；有效 lease 内的运行进度续租 |
| POST | `/hubs/{hub_id}/commands` | 管理员 `HUB_ADMIN_TOKEN` | 受控调试/运维入口；仅允许已上报能力且在最新 inventory 中存在的实例，支持每 Hub 幂等键，最长 24 小时；状态操作 revision 必须递增 |
| GET | `/hubs/{hub_id}/commands/{command_id}` | 管理员 `HUB_ADMIN_TOKEN` | 查询命令状态、attempt、trace 和受限事件摘要；不返回命令 payload 或任意结果/日志 |

已登录工作台的 OpenClaw start/stop 路由会在 Backend 校验实例 owner 后调用相同命令服务，并使用钱包身份写入审计事件；工作台不会获得 Hub 凭证或直接连接 Hub。

Hub 凭证通过数据库管理，不复用旧 `HUB_RUNTIME_NODE_TOKENS`。管理员 API 暂由全局 `HUB_ADMIN_TOKEN` 保护，不是用户级授权方案；不可直接开放为普通工作台命令入口。部署使用 PostgreSQL 并先执行 Alembic migration。

### 命令与收敛语义

- 命令按至少一次投递设计，不能承诺跨进程崩溃的通用 exactly-once。每个命令有全局唯一 `command_id`；Hub 必须先持久化 inbox/执行阶段，再执行副作用。同一 ID 重复投递时返回已保存结果；若 Hub 在副作用后、结果落盘前崩溃，则通过 Adapter inspect/reconcile 确认实际状态再恢复，不可安全恢复的操作报告 `unknown` 并要求显式处置。
- `start_instance`、`stop_instance`、`restart_instance` 等状态操作按实例使用严格递增的 `desired_revision`；更高 revision 会将尚未完成的较低 revision 命令标记为 `superseded`。`inspect_instance` 不推进期望状态 revision。Hub 只对当前有效修订执行收敛；旧修订不能覆盖较新的目标状态。
- 每次领取/重试带递增的 `attempt`。Backend 只在当前 attempt 的 lease 有效时接受其事件对命令状态的更新；lease 过期或 attempt 已被回收的迟到事件仍可留作审计，但不能续租或改变命令结果。同一实例的有效命令 lease 串行化；已 supersede 但仍在有效 lease 中的操作会继续阻挡新操作，直到 Hub 报告终态或 lease 过期。Hub 在执行前复核命令有效性；fencing 保护状态写入不代表能强制终止已开始的本机副作用。
- Hub 客户端当前每轮只领取一条命令，并为长操作在独立线程中续租；续租明确失败或 lease 即将到期时，会向 Adapter 发出协作取消请求、等待本机操作停止，并将结果记录为 `unknown`。真实 Adapter 必须将取消请求映射为有界的子进程/Runtime 中断与清理；不能确认本机动作已停止时，不得继续领取下一条命令。真实 Adapter 尚未实现前，此机制只由可控测试覆盖。
- 命令带 `expires_at`。Hub 离线恢复后不执行已过期命令；Backend 根据最新期望状态重新创建/派发工作，而非无条件重放旧命令。
- 观测状态必须由 Hub/Adapter 的实际检查证据产生。接受命令不代表成功，成功也不必然等于业务健康。
- Runtime 原生长任务的进度和取消能力由 Adapter 声明；Backend 以可持久化任务呈现，不假设所有 CLI 都支持远程中断或完全相同的 session 语义。

### 安全与权限

- Hub 到 Backend 的通信使用 HTTPS 和每 Hub 独立凭证；提供 enrollment、撤销、轮换和最小权限设计。Hub 只能读取发给自己的命令并上报自己的机器数据。
- v1 命令是有 schema 和权限检查的类型化实例操作：`create_instance`、`inspect_instance`、`start_instance`、`stop_instance`、`restart_instance`、`delete_instance`、`update_instance_model`、`get_instance_logs`、`diagnose_instance`、`pair_whatsapp`。创建和扩展操作只接受 OpenClaw 专用的类型化负载；命令模型不接受任意参数对象或 shell 字段。Runtime task execution 需要另行定义有审批语义的 typed payload，不作为任意 shell 后门。
- 命令负载不包含 Secret 明文。Backend 传递受授权的 Secret 引用，Hub 在本机受控存储中解析；日志、事件和错误响应必须过滤凭证及敏感参数。
- 破坏性操作、工作目录范围、执行审批和 CLI 自身安全策略须由服务端授权策略与 Hub 本地策略共同约束；两端任一拒绝都不能执行。
- 所有用户操作、命令投递、执行结果、权限拒绝和 Hub 注册/撤销均可关联审计主体与 trace ID。

### Adapter 合约

Adapter 首期至少声明：`runtime_type`、版本、发现结果、支持的 capabilities 和健康检查方法。操作按 capability 单独定义输入、输出、超时、取消语义和错误类型；执行与恢复方法接收 Hub 执行上下文，必须定期检查取消请求，并在返回前确认本机子操作已停止。未经实现的操作必须明确 unsupported。不得用 shell fallback 伪装成通用兼容层。

Codex CLI、Claude Code CLI 和 OpenClaw 应分别验证真实的本地安装、配置、会话/任务、状态与日志接口，再决定可统一的领域字段。只把三个 Runtime 实际共同支持的部分放进公共模型，其余保留 Adapter 专属扩展字段。

## 仓库现状与迁移

| 现状 | 目标变化 |
|---|---|
| `RuntimeNode` 保存旧节点机器元数据；另有 Hub 服务端表、协议客户端和 OpenClaw 启停 Adapter | 增加 Hub 安装/升级流程、凭证轮换和远程生产端到端验证 |
| Runtime API 和旧 `/agent` Messenger API 的 OpenClaw 实例操作已通过 Hub command；结果/事件回写控制面目录 | 增加 Hub 安装/升级流程、凭证治理、主动脱离进程组的隔离策略和远程生产验收；把 profile/config 以更细的 typed operations 建模 |
| `runtime_providers.py` 只报告本机 OpenClaw 检测能力 | 转为 Hub 上报的 Adapter 能力目录；Backend 展示每个 Hub 的 Runtime inventory |
| 生命周期 worker 管理 Node Registry 社区应用发布包，属于另一条执行链路 | 明确与 Runtime Hub 协议的边界；如迁入 Hub，应另定任务类型与安全约束，不把两套协议混为一谈 |
| Public Runtime API 以当前钱包 owner 和本机实例为中心 | 逐步改为 Backend 管理的 Hub/Instance 资源和授权命令，不允许浏览器直连 Hub |

分阶段实施：

1. 已建立 Runtime/Hub/Instance/命令/事件消息模型、JSON Schema、凭证管理 API、服务端持久化、SQLite inbox/outbox 客户端和 OpenClaw typed Adapter。
2. Runtime API 和旧 Messenger API 的创建、删除、模型配置、启停、日志、诊断及配对已由 Backend 校验钱包 owner 后下发 Hub 命令；Hub 本地 JSON/Secret Store 和 Backend 控制面目录已分离，Hub 日常运行不依赖 PostgreSQL；同机真实 Backend HTTP 与 Hub 客户端闭环、DingTalk 密文持久化通过测试。
3. 已迁移旧 Messenger `/agent` CRUD/config/logs/diagnose/pair API 到 Hub；前端已展示 Hub inventory/观测数据，客户端已加入本地进程锁、凭证文件热加载和受控命令取消；下一步完善凭证签发/撤销运维、Hub 安装升级和远程多机验收。
4. 按真实 CLI 能力接入 Codex 与 Claude Code Adapter，验证 workspace 隔离、审批、日志脱敏、执行取消和离线恢复；逐步开放对应能力。
5. 补齐多用户 RBAC、Hub 凭证治理、告警、升级/回滚、备份恢复及多机故障演练，再开放多机器生产管理。

每阶段都要同时保留 API、协议契约测试和安全负向测试。阶段 3 的验收要求尤其明确：关闭 Backend 直接访问 OpenClaw 进程/文件的主路径，所有实例操作和状态均经本机 Hub Adapter 往返。

## 未选方案

- **Backend 直接 SSH/远程执行**：凭证和网络边界分散，难以支持受限网络、离线状态、稳定审计与可靠重试。
- **浏览器直连每台机器的 Hub**：将中心身份/权限和证书暴露给浏览器，造成跨域、网络可达性和策略绕过问题。
- **Backend 与 Hub 直接双向入站连接**：要求节点暴露公网或依赖额外网络设施；首版没有必要。
- **一开始引入 Kubernetes 或通用 Agent 框架**：与单机 CLI/OpenClaw 的纳管需求不匹配，会把部署复杂度提前带入核心协议。

## 验收边界

本 ADR 记录目标架构与协议约束。当前 OpenClaw 本机 Adapter 支持全套已迁移的 Messenger 实例操作，Hub 客户端已具备本地状态库进程锁、CLI 超时和受控取消，但尚无 Hub 守护进程安装/升级、工作台 Hub 管理/RBAC、远程机器生产验收或 Codex/Claude Adapter。其余能力只有经过实现、测试和运维验收后，才可更新产品能力声明。
