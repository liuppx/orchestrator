# 部署与运维

本文记录生产环境配置、构建、启停和检查流程。仓库根目录的 README 只保留本地快速开始。

## 配置

在每个环境使用同一个配置路径。首次部署从仓库根目录创建：

```bash
test -f backend/.env || cp backend/.env.template backend/.env
```

编辑 `backend/.env`，按当前环境填写配置。生产至少设置 `ROUTER_API_KEY`、`HUB_SESSION_SECRET` 和 `HUB_INTERNAL_TOKEN`；如果运维需要查询全局 Runtime Node 清单，还要设置独立的 `HUB_ADMIN_TOKEN`。生产环境还应设置 `HUB_PUBLIC_BASE_URL` 为控制台 HTTPS 地址，并配置独立的 `HUB_RUNTIME_DIR` 与 `HUB_INSTANCES_ROOT`。建议将 `HUB_SESSION_COOKIE_SECURE_MODE` 设为 `auto` 或 `always`。该文件包含凭证，应限制为服务账号可读且不要提交到版本库。

## 构建与依赖

在构建机或目标机从仓库根目录执行：

```bash
(cd backend && uv sync)
(cd web && npm ci && npm run build)
```

如需由控制面管理 OpenClaw 实例，按环境准备 OpenClaw：

```bash
bash scripts/setup/openclaw_prepare.sh install
bash scripts/setup/openclaw_prepare.sh configure
bash scripts/setup/openclaw_prepare.sh patch
```

## 服务启停

Orchestrator 控制面以本机 Python 进程运行，由仓库脚本管理；不通过 Docker 镜像或 Compose 启动。打包部署与本地运行均使用同一套脚本：

```bash
bash scripts/starter.sh start
bash scripts/starter.sh restart
bash scripts/starter.sh stop
```

默认访问地址为 `http://127.0.0.1:3900/`。启动脚本从 `backend/.env` 读取服务配置；状态和日志默认保存在 `runtime/`。

## 健康检查

```bash
curl -sS http://127.0.0.1:3900/api/v1/public/health
curl -sS http://127.0.0.1:3900/api/v1/public/ready
curl -sS http://127.0.0.1:3900/api/v1/public/version
bash scripts/doctor_full_stack.sh
```

## 打包发布

注意：此脚本会清空 `output/` 下现有内容。无参数运行时会从 `origin/main` 选择版本、创建 patch tag 并推送到远端；运行前请确认 `output/` 中没有需要保留的文件，并确认远端发布时机。

```bash
bash scripts/package.sh
```

安装包输出到 `output/`。打包脚本从当前 `origin/main` 构建，包含 `backend/`、`web/`、`agents/` 和 `scripts/`，不包含运行中的 `runtime/` 状态。部署到目标机后，按上文创建 `backend/.env`、安装依赖并构建前端，再用 `scripts/starter.sh` 启动服务。

## 目录用途

- [运行时数据分类与保留规划](runtime-data.md)
- 生产数据库首次启用前：在 `backend/.env` 设置 `HUB_DATABASE_URL`，执行 `cd backend && uv run alembic upgrade head`，再按 `runtime-data.md` 的停写、备份和空库导入步骤迁移旧 JSON。
- Warehouse 归档是独立 worker：配置 `YEYING_WAREHOUSE_URL` 和短期 scoped `YEYING_WAREHOUSE_TOOL_TOKEN` 后，在单独进程运行 `cd backend && uv run orchestrator-outbox-worker`；修正永久错误后可用 `uv run orchestrator-outbox-worker --retry-failed --once` 重新排队。未配置凭据时 worker 拒绝启动，不影响控制面。
- 应用生命周期执行器是独立 worker：只有在完成 Node runtime 凭证、`NODE_TRUSTED_PUBLISHER_KEYS`、Docker Compose v2、应用配置权限和反向代理评审后，显式设置 `HUB_RUNTIME_EXECUTOR_ENABLED=true`，再运行 `cd backend && uv run orchestrator-runtime-worker`。Docker Compose 仅用于启动/停止被管理的应用容器，不用于启动 Orchestrator 控制面。worker 只接受精确 SemVer，验证 Node 返回的 checksum、release digest、Ed25519 签名和 runtime 安全子集，然后生成受限 Compose；不会直接执行发布者提交的 Compose 或任意 shell。当前 host port 仍为 release 静态端口，单节点多 Project 调度和租户路由尚未完成。
- `runtime/`：控制面状态、日志和托管智能体实例数据；不要作为源码清理。
- `runtime/instances/<id>/workspace/`：控制面为每个实例生成的独立工作区。
- `runtime/applications/<owner-hash>/<app-id>/`：已验证 release、生成的 Compose 和回滚材料；`runtime/app-config/<owner-hash>/<app-id>.json` 是节点本地 `0600` 配置，不提交 Git、不写任务 payload。
- `output/`：打包产物目录。

## 应用生命周期 worker

Node 端为 Orchestrator Runtime Agent 实例设置随机 token，并将其 SHA-256 配置到 `appStoreAgent.instances[instanceId].tokenSha256`。在 Orchestrator 设置相同的 `NODE_RUNTIME_INSTANCE_ID` 和原始 token。`NODE_TRUSTED_PUBLISHER_KEYS` 必须按发布者 `key_id` 配置 PEM Ed25519 公钥，例如 JSON 字符串映射 `{"publisher-key-id":"-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"}`。公钥需通过 Node 已登记发布者配置独立核对，不接受从 release 本身学习新 key。

示例：

```dotenv
HUB_WORKER_TOKEN=<独立随机 worker token>
HUB_RUNTIME_EXECUTOR_ENABLED=true
NODE_REGISTRY_URL=https://node.example.com
NODE_RUNTIME_INSTANCE_ID=<Node Runtime Agent instance id>
NODE_RUNTIME_AGENT_ID=orchestrator
NODE_RUNTIME_TOKEN=<Node Runtime Agent 原始 token>
NODE_TRUSTED_PUBLISHER_KEYS={"publisher-key-id":"-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"}
HUB_APPLICATIONS_DIR=/var/lib/orchestrator/applications
HUB_APPLICATION_CONFIG_DIR=/var/lib/orchestrator/app-config
HUB_APPLICATION_PUBLIC_BASE_URL=https://apps.example.com
```

凭证应通过服务管理器的 secret/environment 注入，或限制 `backend/.env` 只有服务账号可读。每个 Project/app 的配置文件路径为 `<HUB_APPLICATION_CONFIG_DIR>/<owner-hash>/<app-id>.json`；`owner-hash` 是 `sha256(X-YeYing-Instance UTF-8)[:24]`。文件只包含 runtime manifest 声明的白名单来源，如 `DB_HOST`、`DB_PASSWORD`、`REDIS_HOST` 或 `S3_SECRET_ACCESS_KEY`，目录权限 `0700`、文件权限 `0600`。例如：

```bash
PROJECT_INSTANCE_ID=project-local
OWNER_HASH=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:24])' "$PROJECT_INSTANCE_ID")
install -d -m 700 "$HUB_APPLICATION_CONFIG_DIR/$OWNER_HASH"
chmod 600 "$HUB_APPLICATION_CONFIG_DIR/$OWNER_HASH/ai.json"
```

`ai.json` 由运维人员通过安全渠道创建，结构如 `{"DB_HOST":"127.0.0.1","DB_PASSWORD":"<secret>"}`。不要将真实 secret 放入命令行参数、工单、生命周期请求或 Git。worker 会把值写入每个已安装版本权限为 `0600` 的 `runtime.env`；卸载成功后清除版本部署副本，保留管理员维护的源配置文件。

应用只绑定 `127.0.0.1:<runtime.json.service.host_port>`，需要独立反向代理才能访问。host port 当前来自发布制品，不按 Project 动态分配；相同应用在一个节点上的多 Project 安装会发生端口冲突，多租户公网服务上线前必须完成端口预留和租户路由。容器被限制为只读 rootfs、移除 Linux capabilities、禁用新增特权、限制 256 PIDs/512 MiB/1 CPU，并把 `/tmp` 设为临时目录；需要持久卷或额外权限的应用暂不兼容。
