# Hub 控制面

## 1. 定位

Hub 是本仓库的唯一生产控制入口：

- 钱包登录门禁
- 多实例编排（WhatsApp / DingTalk）
- 每实例独立 profile/端口/目录
- 统一模型配置与运行诊断

## 2. 目录

```text
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
  doctor_full_stack.sh
  setup/openclaw_prepare.sh
```

## 3. 生产部署

配置、构建、启动、打包和健康检查命令统一见[生产部署与运维](operations.md)。默认访问地址为 `http://127.0.0.1:3900/`。

前端约定：

- `frontend/` 是 React 源码目录
- `frontend/dist/` 是前端构建产物目录，由 `npm run build` 输出
- Python backend 统一托管 `frontend/dist/`，并负责 SPA 路由回退
- `frontend/dist/` 不作为源码提交对象；仓库只提交 `frontend/src/` 与构建配置，部署或打包阶段再生成构建产物

认证相关配置建议：

- `HUB_PUBLIC_BASE_URL`：线上填写控制台真实 HTTPS 地址，Hub 会把它写入钱包签名 challenge 的 `domain` / `uri`
- `HUB_SESSION_COOKIE_SECURE_MODE`：`auto` / `always` / `never`，默认 `auto`

## 4. 生命周期运维

生产启停和检查命令见[生产部署与运维](operations.md)。

## 5. 本地开发

本地前后端开发命令见仓库根目录 [README](../README.md)。

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
# 检查 config/hub.env 或 backend/.env 中 ROUTER_API_KEY
```

### Q3: WhatsApp 配对后不回消息

1. UI 打开实例日志与诊断。
2. 查看 `recommended_action` 与证据链。
3. 若提示 `router_auth_missing`，补齐 Router 配置并重启实例。

## 8. Legacy 文档

手工单 profile 路径已归档：`docs/archive/legacy/`
