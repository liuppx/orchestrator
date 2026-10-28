# Orchestrator

智能体运行时编排与管理控制台。仓库包含 Python 后端、React 前端，以及 OpenClaw、Nanobot 等智能体实现。

## 快速开始

环境要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22+ 和 npm。

首次运行，在仓库根目录执行：

```bash
(cd backend && uv sync)
test -f backend/.env || cp backend/.env.template backend/.env
(cd web && npm ci)
```

服务参数只有一套，统一配置在 `backend/.env`；模板是 `backend/.env.template`，本地和生产环境使用同一路径，按环境填写不同的值。不要再创建根目录 Hub/OpenClaw 服务配置文件。使用 PostgreSQL 时，在该文件设置 `HUB_DATABASE_URL`（数据库凭据只保存在本机配置，不要提交）。仅在启用 PostgreSQL 时，首次或每次升级后执行版本化迁移：

```bash
(cd backend && uv run alembic upgrade head)
```

本地开发时在终端一启动后端控制面：

```bash
cd backend
uv run orchestrator
```

在终端二启动前端开发服务器：

```bash
cd web
npm run dev
```

打开 <http://127.0.0.1:5174/>。前端开发服务器会将 `/api` 请求代理到后端 `http://127.0.0.1:3900`。退出时分别在两个终端按 `Ctrl+C`。生产启停、打包和后台 worker 运维见[生产部署与运维](docs/operations.md)。

## 文档

- [Orchestrator 控制面说明](docs/orchestrator.md)
- [生产部署与运维](docs/operations.md)
- [运行时数据规划](docs/runtime-data.md)
- [配置说明](docs/configuration.md)
- [仓库目录结构](docs/repository-layout.md)
- [控制面详细设计](docs/orchestrator-control-plane-detailed-design.md)
