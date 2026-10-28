# Orchestrator 控制面

## 定位

Orchestrator 管理 Agent Runtime、运行节点、工作负载和生命周期任务，不是 Agent 本体或 MCP 服务。核心领域对象、流程、竞品边界与重构阶段见[产品定位与领域架构](product-architecture.md)。

## 当前能力

- 钱包身份验证及用户实例隔离。
- 本机 OpenClaw Runtime 实例创建、启停、日志、WhatsApp 配对和诊断。
- PostgreSQL 持久化运行数据，支持生命周期任务租约、幂等、attempt fencing、重试和 outbox。
- 从 Node Registry 获取经过签名校验的发布制品，并通过独立 worker 执行受限的单节点应用生命周期。
- 交易、消息属于业务应用工作台；MCP Server 属于 Agent 可调用工具，不应显示为 Runtime。

当前控制台仅确认本机 OpenClaw Provider。Nanobot、Hermes、远程节点心跳/调度、RBAC 管理后台和统一的生产审计台尚未完成，不应宣称已经支持任意 Runtime 或多节点生产编排。

## 启动与运维

前后端本地启动命令见仓库根目录 [README](../README.md)。生产部署、worker 和发布包操作见[生产部署与运维](operations.md)。

## 目录

```text
backend/src/orchestrator/  Python API、服务、状态存储与 worker
web/src/                  React 控制台及业务应用工作台
agents/                   Agent 实现、Runtime 工具和业务应用
runtime/                  本地运行状态、实例数据和日志（不提交 Git）
```

## API

### 控制台 Public API

- `GET /api/v1/public/runtime/providers`：已注册 Provider 及其当前可用性。
- `GET /api/v1/public/runtime/instances`：当前钱包可见实例与期望/观测状态。
- `POST /api/v1/public/runtime/instances`：创建本机 OpenClaw 实例。
- `POST /api/v1/public/runtime/instances/{id}/actions/{start|stop}`：启停实例。
- `GET /api/v1/public/runtime/instances/{id}/logs`：查看实例日志。
- `GET /api/v1/public/runtime/instances/{id}/diagnose`：检查实例健康证据。

### 兼容的消息实例 API

`/api/v1/public/agent/instances/*` 继续服务既有消息工作台；新控制面入口使用 `/runtime/*`。

### Project / Node internal API

Project 生命周期接口、worker claim/heartbeat/result、鉴权隔离和发布验证约定见[Agent Runtime Protocol](agent-runtime-design.md)。

## 数据与生产边界

PostgreSQL 保存运行控制面、实例、任务、事件与审计元数据；敏感凭证由本机 secret store 管理；运行目录包含配置、工作区、日志和执行恢复资料。Warehouse 只做异步归档，向量数据库不作为权威状态源。详细说明见[运行时数据规划](runtime-data.md)。
