# Orchestrator

智能体运行时编排与管理控制台。仓库包含 Python 后端、React 前端，以及 OpenClaw、Nanobot 等智能体实现。

## 快速开始

环境要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 22+ 和 npm。

首次运行，在仓库根目录执行：

```bash
(cd backend && uv sync)
test -f backend/.env || cp backend/.env.template backend/.env
(cd frontend && npm ci)
```

分别在两个终端启动服务。

终端一，启动后端 API：

```bash
cd backend
uv run python -m uvicorn hub.app:create_app --factory --reload --host 127.0.0.1 --port 3900
```

终端二，启动前端开发服务器：

```bash
cd frontend
npm run dev
```

打开 <http://127.0.0.1:5174/>。前端会将 `/api` 请求代理到后端 `http://127.0.0.1:3900`。开发服务在各自终端按 `Ctrl+C` 停止。

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
