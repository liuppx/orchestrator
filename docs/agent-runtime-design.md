# Agent Runtime Protocol

## 1. 架构定位

社区智能体、AI 应用和 MCP 工具的运行链路统一为：

```text
Project -> Agent Runtime -> Node Registry
```

- Project 是业务系统，只负责项目、任务、文件、成员权限和业务上下文。
- Agent 是运行控制面，负责智能体实例、工具装配、安装、升级、失败回滚、卸载、健康检查和运行诊断。
- Node 是社区发布中心，只负责应用和智能体登记、审核、版本、release artifact、签名和授权入口。
- AppStore 是历史兼容名称，不能再作为新运行时边界的命名来源。

## 2. Project 到 Agent 的 internal API

Project 通过 `AGENT_INTERNAL_URL` 调用 Agent，鉴权使用与 Agent `HUB_INTERNAL_TOKEN` 一致的 `AGENT_INTERNAL_TOKEN`：

```http
Authorization: Bearer <HUB_INTERNAL_TOKEN>
X-YeYing-Instance: project
```

兼容入口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/internal/installed` | 查询 Project 当前已安装能力 |
| POST | `/api/v1/internal/install` | 创建安装任务 |
| POST | `/api/v1/internal/upgrade` | 创建升级任务 |
| POST | `/api/v1/internal/uninstall` | 创建卸载任务 |
| POST | `/api/v1/internal/hooks/{action}` | 接收用户生命周期 hook |

Agent-native 入口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/internal/project/agents` | 查询 Project 可用 Agent 能力 |
| POST | `/api/v1/internal/project/agents/install` | 创建 Agent 安装任务 |
| POST | `/api/v1/internal/project/agents/upgrade` | 创建 Agent 升级任务 |
| POST | `/api/v1/internal/project/agents/uninstall` | 创建 Agent 卸载任务 |
| POST | `/api/v1/internal/project/hooks/{action}` | 接收 Project hook |

所有接口保持 Project 旧 AppStore 代理可识别的返回结构：

```json
{"code": 200, "message": "success", "data": {}}
```

## 3. Agent 本地状态

当前 Agent Runtime 使用本地状态文件作为最小闭环：

```text
runtime/control-plane/project-agents/state.json
```

该文件记录：

- `installed`：Project 视角的已安装能力镜像。
- `tasks`：安装、升级、卸载任务。
- `hooks`：Project 用户生命周期事件。

这不是最终的任务调度实现，只是协议先行的最小可运行状态。后续可以替换为 SQLite 或受控任务队列，但对 Project 的 API 结构保持稳定。

## 4. Agent 到 Node Registry

安装和升级进入真实执行阶段后，Agent 向 Node 查询：

- release artifact 下载地址。
- digest、签名、公钥和授权结果。
- 配置 schema、端口、路由前缀、依赖声明。
- 健康检查声明和失败回滚策略。

Node 不写 Project 本地状态，不启动生产机进程，不直接管理 `docker/appstore/` 目录。

## 5. 兼容边界

- `APPSTORE_INTERNAL_URL` 等旧配置只能作为 Project 兼容名；新部署使用 `AGENT_INTERNAL_URL`。
- `/api/v1/internal/*` 旧路径保留，避免 Project 旧前端入口立刻失效。
- `/api/v1/internal/project/agents/*` 是新增的 Agent Runtime 正式语义。
- `robot` 命名只保留外部协议字段或隐藏兼容路由；新增代码使用 `agent`。

## 6. 下一步

1. 把本地任务从 pending 状态扩展为 `queued -> running -> healthy -> failed -> rolling_back -> rolled_back`。
2. 接入 Node Registry release 查询、下载、digest 校验和签名校验。
3. 实现安装、升级、卸载的真实执行器。
4. 增加健康检查和失败回滚的可观测日志。
5. Project 增加 AI、MCP、任务自动化和通行证扫码登录的 Agent 能力调用协议。
