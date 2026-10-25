# 部署与运维

本文记录生产环境配置、构建、启停和检查流程。仓库根目录的 README 只保留本地快速开始。

## 配置

从仓库根目录创建服务配置：

```bash
cp config/hub.env.template config/hub.env
```

编辑 `config/hub.env`，至少设置 `ROUTER_API_KEY`、`HUB_SESSION_SECRET`、`HUB_ADMIN_TOKEN` 和 `HUB_INTERNAL_TOKEN`。生产环境还应设置 `HUB_PUBLIC_BASE_URL` 为控制台 HTTPS 地址。

本地开发专用环境配置位于 `backend/.env`，模板为 `backend/.env.template`。`backend/.env` 不应复制进生产配置或提交到版本库。

## 构建与依赖

在构建机或目标机从仓库根目录执行：

```bash
(cd backend && uv sync)
(cd frontend && npm ci && npm run build)
```

如需由控制面管理 OpenClaw 实例，按环境准备 OpenClaw：

```bash
bash scripts/setup/openclaw_prepare.sh install
bash scripts/setup/openclaw_prepare.sh configure
bash scripts/setup/openclaw_prepare.sh patch
```

## 服务启停

```bash
bash scripts/starter.sh start
bash scripts/starter.sh restart
bash scripts/starter.sh stop
```

默认访问地址为 `http://127.0.0.1:3900/`。启动脚本从 `config/hub.env` 读取服务配置，状态和日志默认保存在 `runtime/`。

## 健康检查

```bash
curl -sS http://127.0.0.1:3900/api/v1/public/health
curl -sS http://127.0.0.1:3900/api/v1/public/version
bash scripts/doctor_full_stack.sh
```

## 打包发布

注意：此脚本会清空 `output/` 下现有内容。无参数运行时会从 `origin/main` 选择版本、创建 patch tag 并推送到远端；运行前请确认 `output/` 中没有需要保留的文件，并确认远端发布时机。

```bash
bash scripts/package.sh
```

安装包输出到 `output/`。打包脚本从当前 `origin/main` 构建，包含 `backend/`、`frontend/`、`agents/`、`config/` 和 `scripts/`，不包含运行中的 `runtime/` 状态。部署到目标机后，按上文创建 `config/hub.env`、安装依赖并构建前端，再用 `scripts/starter.sh` 启动服务。

## 目录用途

- [运行时数据分类与保留规划](runtime-data.md)
- `runtime/`：控制面状态、日志和托管智能体实例数据；不要作为源码清理。
- `runtime/instances/<id>/workspace/`：控制面为每个实例生成的独立工作区。
- `output/`：打包产物目录。
