# 运行时数据分类与保留规划

## 结论

`runtime/` 不是可随时重建的缓存目录，而是控制面和受管实例的本地持久化数据目录。删掉它可能会丢失实例注册信息、通道配置、任务记录和恢复材料。它不属于源码：不提交到 GitHub、不放进源码包或发布包，也不应随代码升级覆盖。

当前仓库已通过根 `.gitignore` 忽略各层名为 `runtime/` 的目录。`scripts/package.sh` 只复制明确列出的源码目录，并在归档前拒绝包含任何 `runtime/` 目录的包。应用代码和实例产生的实际数据都留在运行主机。

## 当前代码写入的数据

| 路径 | 数据性质 | 内容与敏感度 | 删除影响 |
| --- | --- | --- | --- |
| `runtime/control-plane/state.json` | 持久化控制面状态，不是缓存 | 默认模型和实例注册表；实例项包含所有者钱包地址、profile、端口、PID、状态与目录。钉钉实例还会把 `client_id` 和 `client_secret` 写入此 JSON，后者是凭证。 | 注册信息丢失；现存进程和 OpenClaw profile 不会因此自动停止，但控制面可能无法再识别或管理它们。 |
| `runtime/control-plane/auth/consumed_challenges.json` | 短期安全状态 | 已消费钱包登录 challenge 的 SHA-256 摘要及过期时间，不存登录私钥或明文签名。默认 challenge TTL 为 300 秒。 | 若在 challenge 仍有效时删除，已消费的签名 challenge 可能在过期前重新通过防重放检查。过期摘要会在后续读取时清理。 |
| `runtime/control-plane/project-agents/state.json` | 持久化集成状态和审计记录 | Project Agent 的 installed 状态、生命周期任务、hook 事件。当前实现把完整请求 payload 写入文件；payload 可能带有任意业务数据或凭证。任务和事件数组目前没有保留上限。 | Project Agent 安装状态和本地任务/事件记录丢失；上游请求本身不会因此回滚。 |
| `runtime/control-plane/control-plane.pid` | 可重建进程元数据 | 控制面进程 PID，用于启停脚本判断服务是否运行。 | 可重新生成；服务停止后可能只留下过期 PID。 |
| `runtime/control-plane/logs/control-plane.out.log` | 运维日志 | 控制面标准输出和错误输出。日志可能包含路径、上游错误或诊断信息；应按敏感运行数据管理。 | 丢失排障历史，不影响实例注册状态。 |
| `runtime/instances/<id>/` | 持久化实例数据 | `config/`、`state/`、`workspace/`、`logs/`、`meta/`。保存该实例的配置、生成的角色提示词、事件、网关输出和配对日志。配对日志可能包含二维码或连接信息。 | 实例的配置、日志和工作区无法从控制面目录恢复；实例的 OpenClaw profile 可能仍存在。 |
| `runtime/control-plane/trash/` | 删除后的恢复归档 | 删除实例时，实例目录及对应 OpenClaw profile 目录会被移动到这里；归档可能含通道凭证、会话数据和日志。当前没有自动清理策略。 | 删除后不可从本地回收；在远端仍运行的进程不会因为删归档自动停止。 |

OpenClaw profile 的正常位置在仓库之外：`$HOME/.openclaw-hub-<instance-id>/`。其中可能含渠道登录态和 OpenClaw 配置。删除实例时，该目录会移动进 `runtime/control-plane/trash/`。备份或清理时必须同时考虑仓库外的 profile 目录。

交易员适配器另有 `agents/custom/trader/runtime/`，用于交易状态、信号、订单和运行批次；它同样是本机运行数据，不是仓库源码。当前通用忽略规则会排除各层的 `runtime/` 目录。

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
2. **敏感字段脱离 JSON**：`client_secret` 和可能含密钥的任务 payload 不应明文持久化。改成密钥管理器/受控 secret store 引用，或采用主机密钥加密的 secret 文件；日志和 hook payload 在落盘前做字段白名单与脱敏。
3. **可靠持久层**：将并发写入的 JSON 注册表和无限增长的 task/hook 数组迁移到带 schema 版本、事务和索引的 SQLite；保留导入/回滚工具，禁止升级直接覆盖数据。
4. **保留策略**：登录 challenge 仅保留到过期；控制面与实例日志设置轮转和容量上限；hook/task 审计设置明确期限；trash 设置可配置保留天数并提供预览与显式确认清理。策略落实前不自动删除现有数据。
5. **权限与验证**：创建目录时使用服务账号所有权和最小权限（目录建议 `0700`、文件建议 `0600`）；增加备份恢复演练、迁移校验、过期清理和发布包排除的自动化测试。

## GitHub 与发布包验证

提交前可在仓库根目录确认：

```bash
git check-ignore -v runtime/control-plane/state.json
git ls-files runtime
```

第一条应显示 `runtime/` 忽略规则，第二条应无输出。打包脚本从 Git worktree 读取版本化源码，不读取工作区的 ignored runtime 数据；归档前还会检查并拒绝包内出现名为 `runtime` 的目录。
