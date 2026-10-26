# Agent Runtime Protocol

## 1. 架构定位

社区智能体、AI 应用和 MCP 工具的运行链路统一为：

```text
Project -> Agent Runtime -> Node Registry
```

- Project 是业务系统，只负责项目、任务、文件、成员权限和业务上下文。
- Agent 是运行控制面，负责智能体实例、工具装配、安装、升级、失败回滚、卸载、健康检查和运行诊断。
- Node 是社区发布中心，只负责应用和智能体登记、审核、版本、release artifact、签名和授权入口。
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

## 4. Agent 到 Node Registry

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
