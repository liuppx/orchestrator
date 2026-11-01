# Orchestrator 控制面

## 定位

Orchestrator 管理 Agent Runtime、运行节点、工作负载和生命周期任务，不是 Agent 本体或 MCP 服务。核心领域对象、流程、竞品边界与重构阶段见[产品定位与领域架构](product-architecture.md)。

## 当前能力

- 钱包身份验证及用户实例隔离。
- Runtime API 和旧 Messenger `/agent` API 的 OpenClaw 创建、删除、模型配置、启停、日志、配对和诊断均由 Hub Adapter 经命令协议执行；Backend 只保留 owner/catalog、授权和结果镜像。
- PostgreSQL 持久化运行数据，支持生命周期任务租约、幂等、attempt fencing、重试和 outbox。
- 从 Node Registry 获取经过签名校验的发布制品，并通过独立 worker 执行受限的单节点应用生命周期。
- 交易、消息属于业务应用工作台；MCP Server 属于 Agent 可调用工具，不应显示为 Runtime。

当前控制台仅确认本机 OpenClaw Provider。Nanobot、Hermes、远程节点心跳/调度、RBAC 管理后台和统一的生产审计台尚未完成，不应宣称已经支持任意 Runtime 或多节点生产编排。

## 目标架构

产品方向是中心 Backend + 每台受管机器一个 Hub：Web 工作台只访问 Backend；Hub 主动通过版本化 HTTPS 协议拉取经过授权的类型化命令，并上报本机 Runtime inventory、观测状态和事件。Backend 管身份、期望状态、命令、审计与跨机器视图；Hub 管本机 Runtime Adapter 与执行。Codex CLI、Claude Code CLI、OpenClaw 的能力分别验证，模型供应商单独建模。

服务端已实现首期 Hub 凭证签发/撤销、心跳、inventory、类型化命令和事件 API；OpenClaw 本机 JSON/Secret Store Adapter 支持全套已迁移的 Messenger 实例操作，运行时控制台和旧 API 只经 Hub 命令执行，不会在 Hub 离线时直连 OpenClaw。它还不是生产 Hub 部署：工作台 Hub 管理、用户级 RBAC、守护进程安装/升级、硬取消和远程多机验收尚未交付。当前 Runtime Node 心跳 API 仍是另一套旧接收端。目标、协议约束及迁移验收见[Hub 架构 ADR](hub-architecture-adr.md)。

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
