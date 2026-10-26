# Orchestrator

智能体运行时编排与管理控制台。仓库包含 Python 后端、React 前端，以及 OpenClaw、Nanobot 等智能体实现。

## 快速开始

环境要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22+ 和 npm。

首次运行，在仓库根目录执行：

```bash
(cd backend && uv sync)
test -f config/hub.env || cp config/hub.env.template config/hub.env
(cd frontend && npm ci)
```

在 `config/hub.env` 配置本地服务参数；使用 PostgreSQL 时设置 `HUB_DATABASE_URL`（数据库凭据只保存在本机配置，不要提交）。仅在启用 PostgreSQL 时，首次或每次升级后先加载同一份服务配置，再执行版本化迁移：

```bash
set -a
source config/hub.env
set +a
(cd backend && uv run alembic upgrade head)
```

使用仓库启动脚本以原生 Python 进程启动后端控制面，不需要 Docker。脚本会在后台运行服务并管理 PID 与日志：

```bash
bash scripts/starter.sh start
```

终端二启动前端开发服务器：

```bash
cd frontend
npm run dev
```

打开 <http://127.0.0.1:5174/>。前端开发服务器会将 `/api` 请求代理到后端 `http://127.0.0.1:3900`。停止前端按 `Ctrl+C`；停止后端运行：

```bash
bash scripts/starter.sh stop
```

需要验证 Warehouse 归档时，在 `config/hub.env` 配置 `YEYING_WAREHOUSE_URL` 和短期 `YEYING_WAREHOUSE_TOOL_TOKEN` 后，另开终端运行：

```bash
set -a
source config/hub.env
set +a
(cd backend && uv run orchestrator-outbox-worker --once)
```

若需要管理已发布的 Node 应用，应用生命周期 worker 是独立的可选进程，运行前置条件见 [运维文档](docs/operations.md)；它使用 Docker Compose 启动被管理的应用容器，不会用 Docker 启动 Orchestrator。

本地后端测试：

```bash
(cd backend && uv run python -m unittest discover -s tests)
```

## 文档

- [控制面说明](docs/hub.md)
- [生产部署与运维](docs/operations.md)
- [运行时数据规划](docs/runtime-data.md)
- [仓库目录结构](docs/repository-layout.md)
- [控制面详细设计](docs/hub-control-plane-detailed-design.md)
