# Hub（Agent 控制面，生产入口）

## 1. 定位

Hub 是本仓库的唯一生产控制入口：

- 钱包登录门禁
- 多实例编排（WhatsApp / DingTalk）
- 每实例独立 profile/端口/目录
- 统一模型配置与运行诊断

## 2. 目录

```text
hub/
  backend/
    .env.template
    pyproject.toml
    src/hub/
  frontend/
    src/
    package.json
    dist/
      index.html  # frontend build output
agents/
  openclaw/
  nanobot/
  custom/
scripts/
  starter.sh
  package.sh
  bootstrap_full_stack.sh
  doctor_full_stack.sh
  setup/openclaw_prepare.sh
```

## 3. 启动（推荐）

```bash
cd /home/administrator/code/hub
bash scripts/bootstrap_full_stack.sh
# 编辑 config/hub.env，填 ROUTER_API_KEY
bash scripts/starter.sh start
```

访问：`http://127.0.0.1:3900/`

前端约定：

- `hub/frontend/` 是 React 源码目录
- `hub/frontend/dist/` 是前端构建产物目录，由 `npm run build` 输出
- Python backend 统一托管 `hub/frontend/dist/`，并负责 SPA 路由回退
- `hub/frontend/dist/` 不作为源码提交对象；仓库只提交 `hub/frontend/src/` 与构建配置，部署或打包阶段再生成构建产物

认证相关配置建议：

- `HUB_PUBLIC_BASE_URL`：线上填写控制台真实 HTTPS 地址，Hub 会把它写入钱包签名 challenge 的 `domain` / `uri`
- `HUB_SESSION_COOKIE_SECURE_MODE`：`auto` / `always` / `never`，默认 `auto`

## 4. 停止与重启

```bash
bash scripts/starter.sh stop
bash scripts/starter.sh restart
```

## 5. 本地开发

开发阶段建议前后端分进程启动：

本地 backend 配置文件：

```bash
cp hub/backend/.env.template hub/backend/.env
```

`.env` 只用于本地开发，不提交到远端。

启动 backend：

```bash
cd hub/backend
uv run python -m uvicorn hub.app:create_app --factory --reload --host 127.0.0.1 --port 3900
```

启动 frontend：

```bash
cd hub/frontend
npm install
npm run dev
```

前端开发服务器会把 `/api` 代理到 `http://127.0.0.1:3900`。

停止开发服务：

- 正常停止：在 backend 和 frontend 各自终端里按 `Ctrl+C`
- 如果端口被旧进程占用，可手动清理：

```bash
lsof -tiTCP:3900 -sTCP:LISTEN | xargs kill
lsof -tiTCP:5174 -sTCP:LISTEN | xargs kill
```

打包或部署后的生命周期操作统一使用 `scripts/starter.sh`。

测试命令：

```bash
cd hub/backend
uv run python -m unittest discover -s tests
```

```bash
cd hub/frontend
npm run build
```

## 6. 核心接口分层

### public
- `GET /api/v1/public/health`
- `GET /api/v1/public/version`
- `GET /api/v1/public/auth/me`
- `POST /api/v1/public/auth/wallet/challenge`
- `POST /api/v1/public/auth/wallet/verify`
- `POST /api/v1/public/auth/logout`
- `GET /api/v1/public/agent/types`
- `GET /api/v1/public/agents`
- `GET /api/v1/public/router/models`
- `GET /api/v1/public/agents/{key}/summary`
- `GET /api/v1/public/agents/{key}/config`
- `PUT /api/v1/public/agents/{key}/config`
- `POST /api/v1/public/agents/{key}/actions/run-once`
- `POST /api/v1/public/agents/{key}/actions/start`
- `POST /api/v1/public/agents/{key}/actions/stop`
- `GET /api/v1/public/agent/instances`
- `POST /api/v1/public/agent/instances`
- `GET /api/v1/public/agent/instances/{id}`
- `DELETE /api/v1/public/agent/instances/{id}`
- `PATCH /api/v1/public/agent/instances/{id}/model`
- `POST /api/v1/public/agent/instances/{id}/start`
- `POST /api/v1/public/agent/instances/{id}/stop`
- `POST /api/v1/public/agent/instances/{id}/pair-whatsapp`
- `GET /api/v1/public/agent/instances/{id}/logs`
- `GET /api/v1/public/agent/instances/{id}/diagnose`

### admin
- 预留，当前默认控制面未开放生产 admin 路由

### internal
- `GET /api/v1/internal/installed`
- `POST /api/v1/internal/install`
- `POST /api/v1/internal/upgrade`
- `POST /api/v1/internal/uninstall`
- `POST /api/v1/internal/hooks/{action}`
- `GET /api/v1/internal/project/agents`
- `POST /api/v1/internal/project/agents/install`
- `POST /api/v1/internal/project/agents/upgrade`
- `POST /api/v1/internal/project/agents/uninstall`
- `POST /api/v1/internal/project/hooks/{action}`

Internal 路由使用 `Authorization: Bearer $HUB_INTERNAL_TOKEN`，同时保留 `Token` 头兼容旧 Project AppStore 代理。

## 7. 常见问题

### Q1: 浏览器打开 127.0.0.1:3900 连接被拒绝

```bash
bash scripts/starter.sh start
curl -sS http://127.0.0.1:3900/api/v1/public/health
```

### Q2: 模型拉取失败

```bash
bash scripts/doctor_full_stack.sh
# 检查 config/hub.env 或 hub/backend/.env 中 ROUTER_API_KEY
```

### Q3: WhatsApp 配对后不回消息

1. UI 打开实例日志与诊断。
2. 查看 `recommended_action` 与证据链。
3. 若提示 `router_auth_missing`，补齐 Router 配置并重启实例。

## 8. Legacy 文档

手工单 profile 路径已归档：`docs/archive/legacy/`
