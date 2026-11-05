# Agent Runtime Protocol

> 本文描述 YeYing Project 到 Orchestrator 的社区应用安装/升级/卸载协议，以及对应的单节点生命周期 worker。文中现存 Runtime Node 心跳和 Node Runtime Agent 都属于这条应用部署链路，不等同于 Backend 与每台机器 Hub 之间的 Runtime 控制协议；Hub 的目标架构和协议边界见[Hub 架构 ADR](hub-architecture-adr.md)。两类任务可能最终由同一台机器上的 Hub 执行，但身份、命令模型和能力验收需分别定义。

## 1. 架构定位

社区智能体、AI 应用和 MCP 工具的控制与制品流转边界如下：

```text
Project -> Orchestrator Control Plane -> Lifecycle Worker -> Runtime Node -> Runtime / Workload
Lifecycle Worker -> YeYing Node Registry -> signed release artifacts
```

当前生命周期 worker 与 Runtime 实例都在控制服务所在主机；跨主机 Runtime Node 目前只有心跳接收能力，图中的远端执行关系是目标架构，不是已交付能力。

- Project 是业务系统，只负责项目、任务、文件、成员权限和业务上下文。
- Orchestrator 是运行控制面，负责智能体实例、工具装配、安装、升级、失败回滚、卸载、健康检查和运行诊断。
- YeYing Node / Node Registry 是社区发布中心，只负责应用和智能体登记、审核、版本、release artifact、签名和授权入口；它不是运行 Agent 的主机。
- Runtime Node 是运行 Agent Runtime 的主机。Runtime Node 心跳 API 属于 Orchestrator；与 Node Registry 的制品 API 是两套不同的边界。
- AppStore 是历史兼容名称，不能再作为新运行时边界的命名来源。

## 2. Project 到 Agent 的 internal API

Project 通过 `AGENT_INTERNAL_URL` 调用 Agent，鉴权使用与 Agent `HUB_INTERNAL_TOKEN` 一致的 `AGENT_INTERNAL_TOKEN`：

```http
Authorization: Bearer <HUB_INTERNAL_TOKEN>
X-YeYing-Instance: project
```

创建任务后 Project 使用 `GET /api/v1/internal/tasks/{task_id}` 查询本 Project 的任务状态。节点执行器使用独立的 `HUB_WORKER_TOKEN`，与 Project internal token 分离：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/v1/internal/tasks/claim` | 使用 `X-Worker-ID` 原子领取任务，可传 `limit` 和 `lease_seconds` |
| POST | `/api/v1/internal/tasks/{task_id}/heartbeat` | 同一 `X-Worker-ID` 延长持有中的租约 |
| POST | `/api/v1/internal/tasks/{task_id}/release` | worker 退出或暂不能执行时归还 pending 队列 |
| POST | `/api/v1/internal/tasks/{task_id}/result` | 持有者提交 succeeded/failed 结果并更新 installed 镜像 |

worker 心跳、释放和结果必须同时携带当前 `X-Worker-ID` 与 claim 返回的 `attempts`（作为 `X-Worker-Attempt`）；租约过期或 generation 不匹配时旧 worker 的写入会被拒绝。`HUB_WORKER_TOKEN` 未配置时这些路由返回 503。

### Runtime Node 心跳与运维清单

Runtime Node 通过单独的节点级 Bearer 凭证上报其 Node Agent 版本、Runtime Provider 能力、资源和标签。凭证由服务端 `HUB_RUNTIME_NODE_TOKENS` 按 `node_id` 配置，支持单个 token 或最多三个并行 token 用于人工轮换；每个 token 至少 32 个字符，不写入数据库。

| 方法 | 路径 | 鉴权与行为 |
| --- | --- | --- |
| POST | `/api/v1/internal/runtime-nodes/{node_id}/heartbeat` | 节点自己的 Bearer token；首次心跳创建节点记录，之后更新心跳和能力 |
| GET | `/api/v1/internal/runtime-nodes` | 运维 `HUB_ADMIN_TOKEN`；读取全局节点清单，Project `HUB_INTERNAL_TOKEN` 无权访问 |

心跳存入 PostgreSQL 的 `runtime_nodes` 表及其 `capacity` JSON 字段。在线状态由服务端当前时间与 `last_seen_at` 计算，`HUB_RUNTIME_NODE_OFFLINE_AFTER_SECONDS` 默认 90 秒；未收到心跳的历史本机节点为 `unknown`。实例元数据写入不会伪造或刷新节点心跳。

当前仓库只提供服务端接收端，没有交付独立 Node Agent 心跳客户端、节点管理 UI、凭证自动签发/撤销，也没有通过心跳接口执行远端 Runtime 的启停或部署。该清单暂不接入普通用户控制台，因为组织/RBAC 隔离尚未实现。生产的跨主机心跳须使用 HTTPS、受控网络和环境注入的随机凭证。

兼容入口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/internal/installed` | 查询 Project 当前已安装能力 |
| POST | `/api/v1/internal/install` | 创建安装任务 |
| POST | `/api/v1/internal/upgrade` | 创建升级任务 |
| POST | `/api/v1/internal/uninstall` | 创建卸载任务 |
| POST | `/api/v1/internal/hooks/{action}` | 接收用户生命周期 hook |

Agent-native 入口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/internal/project/agents` | 查询 Project 可用 Agent 能力 |
| POST | `/api/v1/internal/project/agents/install` | 创建 Agent 安装任务 |
| POST | `/api/v1/internal/project/agents/upgrade` | 创建 Agent 升级任务 |
| POST | `/api/v1/internal/project/agents/uninstall` | 创建 Agent 卸载任务 |
| POST | `/api/v1/internal/project/hooks/{action}` | 接收 Project hook |

所有接口保持 Project 旧 AppStore 代理可识别的返回结构：

```json
{"code": 200, "message": "success", "data": {}}
```

## 3. Agent 状态存储边界

配置 `HUB_DATABASE_URL` 时，Agent Runtime 将 installed/task/hook 状态写入 PostgreSQL；不配置时保留本地 JSON 兼容模式：

```text
runtime/control-plane/project-agents/state.json
```

JSON 文件模式下，该文件记录：

- `installed`：Project 视角的已安装能力镜像。
- `tasks`：安装、升级、卸载任务。
- `hooks`：Project 用户生命周期事件。

线上 PostgreSQL 模式下，任务领取、状态转换、租约、幂等和 retry schedule 由 Orchestrator 的数据库层事务化保存；JSON 仅用于本地兼容，不应扩展为线上任务队列。SQLite 仅可作为单机开发/测试适配器，并与 PostgreSQL 使用同一 schema 和迁移契约。迁移与备份边界见 [`runtime-data.md`](runtime-data.md)。

Warehouse 只承接按对象组织的异步归档和产物，不保存 installed/task/hook 业务状态；向量数据库只可保存从已脱敏对象派生、可以重建的语义索引。详细分类、边界和迁移步骤见 [`runtime-data.md`](runtime-data.md)。

## 4. Runtime Worker 到 YeYing Node Registry

`orchestrator-runtime-worker` 领取内部生命周期任务后，调用 Node 的部署机运行时接口：

```http
GET /api/v1/runtime/releases/{app_id}/{version}
Authorization: Bearer <Node Runtime Agent token>
X-YeYing-Instance: <configured runtime instance>
X-YeYing-Agent: orchestrator
```

worker 必须在 Node 中为 `NODE_RUNTIME_INSTANCE_ID` 配置对应 Agent 实例凭证。Node API 当前只按精确 SemVer 查询，`latest` 会失败；Project 安装请求需指定已发布版本。worker 会检查响应 app/version、完整文件 checksum、release digest、受信任发布者 Ed25519 签名、runtime manifest 和 Compose 安全子集。

受信任发布者公钥由部署者通过 `NODE_TRUSTED_PUBLISHER_KEYS` 显式配置并管理轮换；Node runtime artifact response 不包含公钥，因此不能从待安装制品自身建立信任。Node token 和 Worker token 分离，前者只访问 Node runtime API，后者只领取和报告 Orchestrator task。

已验证 release 存入 `HUB_APPLICATIONS_DIR` 下不可变目录。执行器不运行发布者提供的 Compose 文件，而是根据签名过的 `runtime.json` 生成受限 Compose 文件，只创建单个固定 digest image 服务、runtime.env 和 loopback 端口绑定。`runtime.env` 仅从 Agent 节点管理员管理的 `HUB_APPLICATION_CONFIG_DIR/<owner-hash>/<app_id>.json` 加上配置的 app base URL 注入；文件权限必须为 `0600`，任务请求和 release bundle 都不能注入 secret。卸载默认使用 `docker compose down --remove-orphans`，保留命名卷。

升级先部署新版本并探测声明的 loopback healthcheck；成功后更新本地 current manifest。失败时尝试恢复上一个本地已验签的 release 并再次做 healthcheck。Worker 租约失效或收到 SIGTERM 时会终止正在运行的 Docker Compose 命令；正常关停会把任务归还 pending，旧 worker 不能上报新结果。每个 worker 进程必须使用唯一 `HUB_WORKER_ID`，不配置时自动生成。当前执行器是单节点首期能力，应用端口仍使用 Node runtime manifest 中发布的静态 `host_port`；端口被占用时部署失败，不会覆写其他实例。多 Project 端口预留、租户路由和公网反向代理仍是上线前工作。

Node 不写 Project 本地状态，不启动部署机进程，也不直接管理 Orchestrator `runtime/` 目录。

## 4a. AgentRuntime 制品：市场制品到 Hub 实例

除容器清单外，`runtime.json` 可以声明 `kind: "AgentRuntime"`，表示该制品定义的是一个 Agent Runtime 实例而不是容器。它与容器制品走同一套 checksum、release digest 与 Ed25519 验签，**不得**携带 `compose.yaml`：

```json
{
  "api_version": "yeying.app/v1",
  "kind": "AgentRuntime",
  "app_id": "nightly-digest",
  "version": "1.0.0",
  "runtime_type": "codex",
  "deployment_mode": "scheduled",
  "schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "timezone": "Asia/Shanghai"},
  "task": {"prompt": "Summarize yesterday's commits.", "codex_sandbox": "read-only", "run_timeout_seconds": 600},
  "model": null
}
```

- `runtime_type` ∈ `codex` | `claude-code` | `openclaw`。CLI 运行时使用 `task`（`prompt`、`codex_sandbox` 或 `claude_permission_mode`、`run_timeout_seconds`），恒为 `scheduled`；OpenClaw 使用 `openclaw`（仅 `kind: "whatsapp"` 与可选 `template`），可为 `service` 或 `scheduled`。
- 验签之后，`ReleaseVerifier` 直接用 Hub 协议模型（`HubCliTaskCreatePayloadV1` / `HubOpenClawCreatePayloadV1`）构造创建 payload。制品因此无法表达 Hub 会拒绝的内容：两个 CLI 的 bypass 模式、未知字段、任何 secret。DingTalk 工作负载需要 client secret，故不允许出现在签名公开制品中。
- 实例显示名取 `application.json` 的 `metadata.name["en-US"]`（或首个非空值），回退为 `app_id`。

### Worker 到 Backend 的实例 API

lifecycle worker 不连接数据库、也不直接连接 Hub。它通过 `HUB_WORKER_TOKEN` 调用：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/v1/internal/runtime/instances` | body：`owner_id`、`runtime_type`、`instance_id`、`create_payload`（typed）。按 `instance_id` 幂等：已存在且 owner/runtime 一致则返回 `created=false`，不再下发命令；Hub 离线返回 503；adapter 未申报返回 409 |
| POST | `/api/v1/internal/runtime/instances/{id}/delete` | 不存在返回 `deleted=false, reason=INSTANCE_NOT_FOUND`；否则下发 `delete_instance` 并等待结果 |

两者都由 Backend 经 `MachineHubService` 向配置的 `HUB_ID` 下发 typed 命令并等待终态；命令、事件与审计与控制台发起的操作完全相同。

### 执行器行为

- 实例 id 按 `(runtime_type, app_id, owner)` 稳定；`install` 创建，`upgrade` 先删旧定义再建新定义（Hub 保留 workspace 与运行日志，只有目录项被替换），`uninstall` 删除。容器制品不能原地替换 agent 安装（`RELEASE_KIND_CHANGED`）。
- `current.json` 记录 `instance_id` 与 `runtime`；失败报告对 agent 安装不探活容器，若升级时旧定义已删而新建失败，报告 `previous_removed=true` 并清空 `current.json`。
- 错误分类：`HUB_UNAVAILABLE`（可重试）、`HUB_REJECTED`、`HUB_COMMAND_FAILED`、`INVALID_AGENT_RUNTIME`、`INSTANCE_NOT_FOUND`、`AGENT_GATEWAY_NOT_CONFIGURED`。
- Project 安装的实例归属 Project owner，不出现在无关钱包的控制台列表；Project 通过 `GET /api/v1/internal/installed` 查看。

## 5. 兼容边界

- `APPSTORE_INTERNAL_URL` 等旧配置只能作为 Project 兼容名；新部署使用 `AGENT_INTERNAL_URL`。
- `/api/v1/internal/*` 旧路径保留，避免 Project 旧前端入口立刻失效。
- `/api/v1/internal/project/agents/*` 是新增的 Agent Runtime 正式语义。
- `robot` 命名只保留外部协议字段或隐藏兼容路由；新增代码使用 `agent`。

## 6. 当前执行闭环与下一步

1. task 以 `pending -> running -> succeeded/failed` 持久化，带 PostgreSQL lease、heartbeat、attempt fencing、worker-owned result、幂等键和可持久化 retry schedule；Node/容器临时故障以指数退避重试，达到最大尝试次数才最终失败。installed 镜像按 Project owner 隔离。升级失败并确认回滚成功时，旧版本仍保持 installed。
2. task/hook 事件事务写入 PostgreSQL outbox；`orchestrator-outbox-worker` 可异步归档 Warehouse。
3. 首期 Node runtime release 查询、下载限额、checksum/digest/Ed25519 验签、受限 Compose 生成、install/upgrade/uninstall、loopback healthcheck 和本地旧版本回滚已实现。`HUB_RUNTIME_EXECUTOR_ENABLED` 默认关闭；不开启前不会产生部署副作用。
4. 上线前还需完成真实 Node 凭证联调、发布者 key 轮换治理、按节点端口预留/租户路由、外部 reverse proxy、应用配置/secret 管理、Docker 资源配额、备份恢复与告警。当前 `latest` 不支持，不应将单节点首期部署宣称为多租户生产就绪。
5. Project 增加 AI、MCP、任务自动化和通行证扫码登录的 Agent 能力调用协议。
