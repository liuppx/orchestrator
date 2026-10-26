# 运行时数据分类与保留规划

## 结论

`runtime/` 不是可随时重建的缓存目录，而是控制面和受管实例的本地持久化数据目录。删掉它可能会丢失实例注册信息、通道配置、任务记录和恢复材料。它不属于源码：不提交到 GitHub、不放进源码包或发布包，也不应随代码升级覆盖。

当前仓库已通过根 `.gitignore` 忽略各层名为 `runtime/` 的目录。`scripts/package.sh` 只复制明确列出的源码目录，并在归档前拒绝包含任何 `runtime/` 目录的包。应用代码和实例产生的实际数据都留在运行主机。

## 当前代码写入的数据

| 路径 | 数据性质 | 内容与敏感度 | 删除影响 |
| --- | --- | --- | --- |
| `runtime/control-plane/state.json` | 兼容模式的持久化控制面状态，不是缓存 | 未设置数据库时保存默认模型和实例注册表；实例项包含所有者钱包地址、profile、端口、PID、状态与目录。钉钉 `client_id` 可在注册表中保存，`client_secret` 由代码迁移到同一 runtime 下权限收紧的 `secrets/` 文件，不再写回 JSON。 | 注册信息丢失；现存进程和 OpenClaw profile 不会因此自动停止，但控制面可能无法再识别或管理它们。 |
| `runtime/control-plane/secrets/` | 节点本地凭证文件 | 内容寻址的钉钉 secret 文件；目录权限 `0700`、文件权限 `0600`，数据库和 JSON 注册表只保存引用。 | 钉钉实例不能重新生成 profile 配置；需从受控凭证备份恢复。 |
| `runtime/control-plane/auth/consumed_challenges.json` | 短期安全状态 | 已消费钱包登录 challenge 的 SHA-256 摘要及过期时间，不存登录私钥或明文签名。默认 challenge TTL 为 300 秒。 | 若在 challenge 仍有效时删除，已消费的签名 challenge 可能在过期前重新通过防重放检查。过期摘要会在后续读取时清理。 |
| `runtime/control-plane/project-agents/state.json` | 兼容模式的持久化集成状态和审计记录 | 未设置数据库时保存 Project Agent 的 installed 状态、生命周期任务、hook 事件；当前兼容文件路径也会做敏感字段过滤。 | Project Agent 安装状态和本地任务/事件记录丢失；上游请求本身不会因此回滚。 |
| `runtime/applications/<owner-hash>/<app-id>/` | 已验签的 Node release 和部署镜像 | bundle 原始文件、运行时生成的最小 `runtime.compose.yaml`、权限为 `0600` 的 `runtime.env`、当前 release manifest。密钥只从节点的受限 app config 文件读取。 | 删除后无法按本地当前版本回滚；容器可能仍运行，但控制面会失去部署材料。先备份并核对 Docker/数据库状态后再清理。 |
| `runtime/app-config/<owner-hash>/<app-id>.json` | 节点本地应用配置/secret | 仅允许 runtime manifest 声明的白名单环境变量名；文件必须 `0600`，父目录由服务账号管理。数据库、task、Node release 不保存这些值。 | 应用可能无法启动或重建；需从受控密钥来源恢复。 |
| `runtime/control-plane/control-plane.pid` | 可重建进程元数据 | 控制面进程 PID，用于启停脚本判断服务是否运行。 | 可重新生成；服务停止后可能只留下过期 PID。 |
| `runtime/control-plane/logs/control-plane.out.log` | 运维日志 | 控制面标准输出和错误输出。日志可能包含路径、上游错误或诊断信息；应按敏感运行数据管理。 | 丢失排障历史，不影响实例注册状态。 |
| `runtime/instances/<id>/` | 持久化实例数据 | `config/`、`state/`、`workspace/`、`logs/`、`meta/`。保存该实例的配置、生成的角色提示词、事件、网关输出和配对日志。配对日志可能包含二维码或连接信息。 | 实例的配置、日志和工作区无法从控制面目录恢复；实例的 OpenClaw profile 可能仍存在。 |
| `runtime/control-plane/trash/` | 删除后的恢复归档 | 删除实例时，实例目录及对应 OpenClaw profile 目录会被移动到这里；归档可能含通道凭证、会话数据和日志。当前没有自动清理策略。 | 删除后不可从本地回收；在远端仍运行的进程不会因为删归档自动停止。 |

OpenClaw profile 的正常位置在仓库之外：`$HOME/.openclaw-hub-<instance-id>/`。其中可能含渠道登录态和 OpenClaw 配置。删除实例时，该目录会移动进 `runtime/control-plane/trash/`。备份或清理时必须同时考虑仓库外的 profile 目录。

交易员适配器另有 `agents/custom/trader/runtime/`，用于交易状态、信号、订单和运行批次；它同样是本机运行数据，不是仓库源码。当前通用忽略规则会排除各层的 `runtime/` 目录。

## 存储选型

未设置 `HUB_DATABASE_URL` 时，系统保留单机文件存储模式：控制面把实例注册表整份读写为 JSON，Project Agent 把状态数组整份序列化为 JSON。这是本地兼容路径，不适合多人或多进程在线生产。

首期数据库适配已经提供：设置 `HUB_DATABASE_URL` 后，实例注册表、生命周期 task/hook 和 challenge 消费记录通过 SQLAlchemy 访问；版本化 migrations 提供初始 schema 及后续字段变更。未设置该变量时仍使用 JSON，便于本地快速开始。生产启用前先在仓库根加载部署配置，再运行版本化迁移：

```bash
set -a
source config/hub.env
set +a
(cd backend && uv run alembic upgrade head)
```

迁移旧数据时先停写并备份，目标库需为空，然后导入实例、Project Agent 状态和仍有效的 challenge 摘要：

```bash
(cd backend && uv run orchestrator-import-runtime-json "$HUB_RUNTIME_DIR/state.json" \
  --database-url "$HUB_DATABASE_URL" \
  --runtime-dir "$HUB_RUNTIME_DIR" \
  --instances-root "$HUB_INSTANCES_ROOT")
```

首期 secret adapter 是节点本地文件，不是 KMS/云 Secret Manager；文件权限不等于静态加密。当前 OpenClaw profile 配置也需要收到钉钉明文凭证。正式公网运营前应完成外部 Secret Manager/KMS adapter、profile 凭证注入/轮换方案和备份加密；数据库模式目前适合单节点控制面，不代表已经具备多节点调度能力。

这让本地试用不需要额外服务，但不提供适合多人或多进程写入的事务、并发更新、索引和任务领取语义。因而文件 JSON 是当前实现的简化，不是长期在线状态的推荐事实源。

| 技术 | 在本项目中的定位 | 不负责什么 |
| --- | --- | --- |
| PostgreSQL | 生产控制面元数据事实源：实例、节点/运行时注册、任务、状态转换、幂等键、challenge 摘要和审计元数据。提供事务、唯一约束、并发协调和可查询性。 | 不保存 OpenClaw 正在使用的 profile 目录、活跃会话文件和大量日志正文。 |
| SQLite | 可选的本机单进程开发/测试适配器，方便离线开发和单机演示。若启用，必须通过同一 repository/schema/migration 契约，并在 CI 验证生产 PostgreSQL 行为。 | 不作为多副本或共享在线生产实例的协调数据库。 |
| Warehouse | 大小合适且需要长期保存的对象：脱敏审计导出、备份 manifest、Agent 产物和归档日志；由异步任务写入，数据库记录对象路径、checksum 和状态。 | 不作为实例注册表、任务队列、会话状态或 secret store。其内部 PostgreSQL 不是 Orchestrator 可直接访问的业务数据库。 |
| 向量数据库 | 可选的派生索引，用于未来对知识、已脱敏历史诊断或文档做语义检索。源对象仍由文件存储或 Warehouse 持有，索引可以重建。 | 不作为实例状态、凭证、端口分配、任务去重或生命周期审计的事实源。 |
| 本机持久卷 | 节点本地活跃 OpenClaw profile、workspace、配置文件和需要进程直接读写的状态；PostgreSQL 保存其 `node_id` 和受控路径/引用。 | 不等于仓库 checkout；必须有独立权限、备份和节点归属。 |

### 为什么不直接把控制面状态写入 Warehouse

Warehouse 不是“把数据库换成 HTTP 的通用行存储”。当前面向 Agent 的外部契约是受身份、UCAN 路径、动作、配额和审计约束的对象 API：列目录、查对象元数据、读对象以及写小对象。`warehouse.object.put` 单对象上限为 5 MiB，默认只创建；覆盖需要显式开启并可用 ETag `ifMatch` 做单对象条件写。上传会话 Tool 尚未对外开放，契约也没有 SQL 查询、事务跨多对象提交、行锁/租约、原子领取任务或业务生命周期状态机。

因此 Warehouse 很适合存档单个 manifest、脱敏日志包或运行产物；不适合由每次启动/停止操作读改写一个共享 JSON 对象来模拟数据库。这样做会让列表/过滤退化成对象遍历，实例状态与任务事件无法一起提交，也无法可靠实现并发领取和幂等状态转换。Warehouse 自身使用 PostgreSQL 实现部分内部元数据，不代表外部调用方可以连接其数据库或共用内部表；Orchestrator 必须使用自己的 schema/database，并通过 Warehouse 公共授权接口管理对象。

### 为什么不使用向量数据库

本项目当前的核心查询是精确关系和状态转换，例如按实例 ID 查状态、校验实例所有权、分配未占用端口、领取 pending 任务、去重 webhook。这些要求确定性过滤、唯一约束和事务，而不是“语义上最相似”的结果。向量库既不替代 SQL 约束，也不适合保存秘密或运行配置。只有当产品明确要做“相似故障检索”或 Agent 知识记忆时，再把经过脱敏的文本索引到向量存储，并保留可回源的原始记录。

## 目标架构决策

对面向在线运营的 Orchestrator，推荐采用以下事实源分工：

```text
PostgreSQL   -> 控制面业务状态、任务状态机、幂等与审计元数据
Secret Store -> 钉钉/渠道凭证及其他长期密钥
本机持久卷   -> 绑定到具体运行节点的 OpenClaw profile 和活跃工作区
日志平台     -> 在线日志查询、轮转和告警
Warehouse    -> 异步归档、manifest、脱敏审计导出和较大运行产物
向量索引     -> 可选的知识/历史故障检索派生数据
```

PostgreSQL 是生产控制面的数据库选择；单机开发若使用 SQLite，只能是便于启动的开发适配器，不能变成第二套生产事实源。数据库记录中的 secret 只保存 Secret Store 引用，不保存明文。任务事件写入 PostgreSQL 后，再通过 outbox/后台 worker 异步导出到 Warehouse；Warehouse 暂时不可用时不能阻塞实例启停。

## PostgreSQL 逻辑模型（首期 schema 已落地）

下面的实体已由 SQLAlchemy model 和版本化 migration 建立；后续字段变化必须新增 migration，不得修改已应用的 migration。task 已支持 PostgreSQL 原子领取、租约续期、释放、持有者校验和结果回报，节点 worker 使用独立 `HUB_WORKER_TOKEN`；task 事件与 hook 在同一事务写入 outbox。`orchestrator-outbox-worker` 使用 `YEYING_WAREHOUSE_URL` 和短期 scoped `YEYING_WAREHOUSE_TOOL_TOKEN`，通过 Warehouse `warehouse.object.put` 异步归档；Warehouse 不可用时按指数退避，不阻塞控制面。

| 实体 | 关键字段/约束 | 用途 |
| --- | --- | --- |
| `runtime_nodes` | `id`、`node_key`（unique）、`status`、`last_seen_at`、`capacity`、`version` | 标记实例实际运行在哪个受控节点；本机部署先只有一个 node。 |
| `agent_instances` | `id`、`owner_id`、`node_id`、`runtime_type`、`profile_ref`、`workspace_ref`、`port`、`desired_state`、`observed_state`、`revision`、时间戳 | 实例目录和进程状态的控制面索引。`(node_id, port)` 唯一；不存长期 secret。PID 和 observed state 视为可过期观测值，不能当作唯一的活性证明。 |
| `instance_configs` | `instance_id`（FK）、`revision`、非敏感 `config_json`、`secret_refs`、更新时间 | 保存可版本化的配置与 Secret Store 引用；凭证正文不进入 JSONB。 |
| `lifecycle_tasks` | `id`、`instance_id`、`action`、`status`、`idempotency_key`、尝试次数、租约持有者/期限、输入摘要、结果码、时间戳 | 持久任务队列。`(owner_id, idempotency_key)` unique；Worker 用 `FOR UPDATE SKIP LOCKED` 或等价 claim 语义领取任务。 |
| `task_events` | `id`、`task_id`、`event_type`、脱敏 metadata、`request_id`、时间戳 | 追加式状态迁移和执行证据，payload 使用白名单，避免保存任意请求体。 |
| `consumed_challenges` | `digest`（PK）、`expires_at`、`consumed_at` | 通过唯一插入原子消费 challenge；定期删除过期摘要。 |
| `audit_events` | actor、action、resource、outcome、trace/request ID、脱敏 metadata、时间戳 | 查询安全与运维操作。按保留期归档，不能用全量请求 payload 代替审计 schema。 |
| `outbox_events` | `id`、event type、object path、checksum、delivery status、attempts、lease、remote ETag、错误和重试时间 | 把需归档对象可靠地异步发送到 Warehouse，避免数据库事务提交成功但归档消息丢失。 |

数据库事务只覆盖 PostgreSQL 内部写入，不可能和创建文件目录、配置 OpenClaw 或启动 OS 进程形成一个原子事务。实例创建/升级/启动应采用 saga/reconcile 模型：事务记录期望状态和任务，Worker 幂等执行外部副作用，再写入观测结果；中断后从任务状态继续或补偿，不能假装“数据库回滚”等于进程和文件回滚。

端口分配也需要两层保护：数据库用 `(node_id, port)` 唯一约束避免控制面重复预留，节点执行器启动前再检查 OS 端口是否实际可绑定。若节点报告冲突，释放/重新分配 reservation 并记任务事件。

## JSON 迁移与切换步骤（规划）

迁移应保持单一写入事实源，不做长期 dual-write：

1. **盘点与备份**：停止 Orchestrator 和实例生命周期操作；加密备份 `HUB_RUNTIME_DIR`、`HUB_INSTANCES_ROOT`、trash、关联 OpenClaw profile 和密钥来源。记录实例数、ID、任务数、目录路径及文件 checksum。
2. **先处理凭证**：为每个钉钉 secret 创建 Secret Store 条目，权限限定为对应实例；数据库只导入 secret reference。逐个启动/探测验证后再安排旧明文配置和备份轮换清理。
3. **建立 schema**：应用版本化 migration，创建逻辑模型、unique/FK/index 约束、任务状态迁移表和 outbox。先在脱敏副本上演练升级与回滚。
4. **导入只读核对**：导入脚本保留现有 instance/task ID；challenge 只导入仍有效的摘要。比较数量、关键字段和 workspace/profile 路径，不在日志中输出 secret 或完整 payload。
5. **停机切换**：再次停止旧服务，生成最终备份和增量，执行导入校验后切换数据库配置。切换后只允许新版本写 PostgreSQL；旧 JSON 只读保留一段回退窗口。
6. **验证与观察**：逐实例检查 owner、node、目录、profile、端口、desired/observed state；验证登录 challenge 防重放、重复 task 幂等、任务重启恢复、instance start/stop 和审计查询。检查成功后再宣布切换完成。
7. **回退边界**：若切换后尚未执行新的外部副作用，可停止新服务并回到切换前快照。若已启动/停止/升级实例，不可简单回滚数据库或恢复旧 JSON；应以前向修复/reconcile 为主，或从 PostgreSQL 导出完整变更并人工核对后回退。
8. **异步归档**：开启 outbox Worker 后，将脱敏事件、manifest 和适合长期保存的产物异步写入 Warehouse；记录 object path、ETag、checksum 和 delivery state。归档失败可重试，不影响在线状态读写。

## 当前本地检查结果

此文档编写时，本地根 `runtime/` 只有：

- `control-plane/state.json`：默认模型配置，实例列表为空。
- `control-plane/auth/consumed_challenges.json`：一条有过期时间的 challenge 摘要。
- `instances/` 目录为空；没有 Project Agent 状态文件、运行日志或实例归档。

这些文件已被 Git 忽略，`git ls-files runtime` 无输出。当前数据不会进入本次提交。

## 存储与备份规则

1. 源码仓库只保存代码、无密钥模板和可重建的静态定义。禁止把本地 `runtime/`、实例配置、日志、profile、交易结果或凭证强行加入 Git。
2. 当前本地开发默认将状态放在仓库根 `runtime/`，由 `.gitignore` 排除。生产环境必须在 `config/hub.env` 显式设置独立的绝对路径：`HUB_RUNTIME_DIR` 与 `HUB_INSTANCES_ROOT`；不要依赖仓库内默认目录。
3. 建议生产持久状态目录使用 `/var/lib/orchestrator/control-plane` 和 `/var/lib/orchestrator/instances`，目录归专用服务账号所有且权限最小化。日志暂时跟随 `HUB_RUNTIME_DIR/logs/`；后续可拆至 `/var/log/orchestrator/` 并接入日志轮转。
4. 备份应加密并限制访问。需恢复实例时，成套保存控制面注册状态、`HUB_INSTANCES_ROOT`、删除归档，以及对应的 OpenClaw profile 和密钥来源。生产备份前先停止服务或使用一致性快照；交易和 Project Agent 状态不能当作临时缓存删除。
5. 开发环境可以重建依赖和前端产物，但不能据此推断运行状态可重建。任何清理操作先区分 PID、过期 challenge、日志、任务记录、活跃实例和 trash，不使用 `rm -rf runtime` 作为通用清理方式。

## 后续治理计划

以下是目标方案，不代表清理、加密或迁移已经实现：

1. **存储迁出仓库**：为 macOS 开发环境采用 `~/Library/Application Support/orchestrator/`；Linux 开发环境采用 `${XDG_STATE_HOME:-$HOME/.local/state}/orchestrator/`。生产使用 `/var/lib/orchestrator/`。分别配置控制面状态与实例数据路径，停止服务后迁移、校验并保留回滚副本，再切换默认路径。
2. **敏感字段脱离 JSON**：已将控制面 `client_secret` 拆到节点权限受限文件，任务/hook payload 做敏感键过滤。后续替换为 KMS/Secret Manager，并解决 OpenClaw profile 内明文配置和安全轮换；日志和 hook payload 还需经过字段白名单审查。
3. **可靠持久层**：已建立 PostgreSQL/SQLite 共享 schema、Alembic migration、JSON 导入工具、task 原子领取/租约和 Warehouse outbox worker。仍需补导入校验/回滚演练、审计保留策略和生产监控；SQLite 仅供本地开发。
4. **保留策略**：登录 challenge 仅保留到过期；控制面与实例日志设置轮转和容量上限；hook/task 审计设置明确期限；trash 设置可配置保留天数并提供预览与显式确认清理。策略落实前不自动删除现有数据。
5. **权限与验证**：创建目录时使用服务账号所有权和最小权限（目录建议 `0700`、文件建议 `0600`）；增加备份恢复演练、迁移校验、过期清理和发布包排除的自动化测试。

## GitHub 与发布包验证

提交前可在仓库根目录确认：

```bash
git check-ignore -v runtime/control-plane/state.json
git ls-files runtime
```

第一条应显示 `runtime/` 忽略规则，第二条应无输出。打包脚本从 Git worktree 读取版本化源码，不读取工作区的 ignored runtime 数据；归档前还会检查并拒绝包内出现名为 `runtime` 的目录。
