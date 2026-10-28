# Orchestrator 产品定位与领域架构

更新时间：2026-10-28
范围：面向需要在自有节点部署、运行和维护异构 Agent Runtime 的社区与小型平台团队。

## 产品定位

Orchestrator 是 Agent Runtime 的控制面，不是 Agent 本体、Agent 开发框架、MCP 服务或通用工作流引擎。它负责把“哪里运行、运行什么、期望什么状态、实际观察到什么状态、如何安全操作和排障”组织成一个可审计的运维闭环。

建议的核心对象关系：

```text
Organization / Project
  └── Runtime Node
       └── Runtime Provider (OpenClaw, Nanobot, ...)
            └── Runtime Instance
                 └── Workload (Agent / application)
                      └── Tools (including MCP servers)
```

- **Runtime Node**：运行 Agent Runtime 的主机或受信任执行节点，报告心跳、容量、版本和可用执行器。它不同于 YeYing Node 社区制品发布中心。
- **YeYing Node / Node Registry**：社区应用制品的登记、审核、版本、签名和下载服务，不是 Agent Runtime 的运行主机。
- **Runtime Provider**：由 adapter 实现的管理协议和能力集合，不等同于一个进程。
- **Runtime Instance**：某个 Runtime Node 上具有独立配置、目录、资源和生命周期的运行时实例。
- **Workload**：运行在实例中的 Agent、社区应用或业务工作负载。
- **Tool / MCP Server**：Agent 在运行期调用的外部能力。Orchestrator 可以登记其配置引用，但 MCP Server 本身不是 Runtime Provider。
- **Lifecycle Task**：部署、升级、启停和卸载等异步操作的请求、租约、重试与结果。
- **Runtime Event / Audit Event**：状态观测、操作者动作和执行结果的时间序列证据。

期望状态与观测状态必须分开保存。用户点击启动只表示 `desired_state=running`；只有 adapter/Runtime Node 给出可验证的健康证据，才能把 `observed_state` 标记为 running。启动失败仍保留期望状态和错误证据，供重试、诊断与告警使用。

## 核心流程

1. **连接节点**：注册 Runtime Node 身份，校验节点凭证与能力，接收初始心跳；离线节点不能被当作可部署目标。
2. **检测运行时**：Runtime Provider adapter 报告版本、能力、约束和健康状态；未适配的运行时只显示“未接入”，不允许伪装成可用。
3. **创建工作负载**：选择发布版本、目标 Runtime/Runtime Node 与经过权限校验的配置引用；将不可变输入记录为 Lifecycle Task。
4. **执行与收敛**：Runtime worker 领取带 fencing 的任务，验证发布来源和制品，再执行部署；控制面持续比较期望状态与观测状态。
5. **日常运维**：按状态筛选实例，查看最近事件、日志和健康检查；启停、升级、回滚等动作生成审计事件并返回可追踪的任务结果。
6. **故障恢复**：错误显示具体失败阶段、证据和下一步；可重试操作有幂等键、租约、退避和最大重试限制，破坏性操作需要确认与权限检查。

## 市场参考

Orchestrator 处于几个成熟类别的交叉处，不应照搬单一产品的全部信息架构。

| 产品 | 类别与强项 | 对 Orchestrator 的借鉴 | 不应混为一谈的边界 | 定价观察 |
|---|---|---|---|---|
| [Dify](https://docs.dify.ai/en/introduction) | Agent/LLM 应用开发平台，覆盖模型、知识、工具、工作流与应用运行 | 把配置、发布和运行态连起来；新手路径要从一个真实工作负载开始 | 重点是构建 AI 应用，不是多 Runtime 节点生命周期控制面 | 官方 Cloud 有 Free 及付费计划；自托管成本由用户基础设施承担。具体金额按官方页面实时核验 |
| [LangSmith](https://docs.smith.langchain.com/) | Agent 开发、评测、追踪和部署生态；LangGraph Deployment 管理 Agent 服务 | 以运行追踪、评估和可观测性提升生产排障效率 | 主要与 LangChain/LangGraph 生态关联，不是通用宿主机管理面 | 官方价格页展示 Free 与 Plus，Plus 页面显示 `$39/seat/month`，另有按用量收费项；价格可能变化 |
| [Portainer](https://docs.portainer.io/) | Docker、Kubernetes 与 Edge 的容器/基础设施管理 | 以环境/节点为运维主对象；明确节点连接状态和可执行能力 | 管理容器基础设施，不提供 Agent 领域对象、提示词或 Agent 评测 | Community 免费；Business/Enterprise 由节点规模和授权方案决定，见官方价格页 |
| [Temporal](https://docs.temporal.io/) | Durable execution 工作流引擎，擅长任务持久化、重试和故障恢复 | 任务需可恢复、幂等、可观察；worker 租约与持久化重试不可只依赖内存 | 是应用开发者使用的工作流执行平台，不是运行时管理 UI/产品 | Temporal Cloud 按 Actions 等用量计费；官方页面给出 `$50 / million Actions` 示例及新账户额度 |

### 能力对照

评级说明：完整表示产品主要职责覆盖该能力；部分表示可通过特定生态、插件或有限场景实现；不适用表示不是该产品核心职责。Orchestrator 列仅描述当前仓库实现，不代表目标规划。

| 用户关心的能力 | Orchestrator 当前 | Dify | LangSmith | Portainer | Temporal |
|---|---|---|---|---|---|
| Agent 应用/工作流构建 | 无 | 完整 | 部分 | 不适用 | 不适用 |
| Agent tracing / evaluation | 无 | 部分 | 完整 | 不适用 | 不适用 |
| Runtime/Node 生命周期管理 | 部分：本机 OpenClaw；有 Runtime Node 心跳接收 API 和单节点应用 worker | 部分：AI 应用运行 | 部分：LangGraph Deployment | 完整：容器/集群 | 部分：任务 worker |
| 异构 Agent Runtime adapter | 无：当前控制台仅 OpenClaw adapter | 不适用 | 部分：LangGraph 生态 | 不适用：以容器环境为主 | 不适用 |
| 持久化任务、重试与 fencing | 部分：PostgreSQL 任务 lease/attempt 已实现，实际执行器为单节点 | 部分 | 部分 | 不适用 | 完整 |
| 发布制品校验 | 部分：Node release digest/checksum/Ed25519 验证已实现 | 部分 | 部分 | 部分：镜像/stack | 不适用 |
| MCP Server 作为 Agent 工具 | 不在控制面核心职责 | 完整/部分，取决于应用配置 | 部分 | 不适用 | 不适用 |

### 定位图

横轴表示主要管理对象从“应用定义”到“运行基础设施”，纵轴表示对 Agent 领域的专用程度。示意定位基于公开产品职责，是类别判断，不是第三方性能评测。

```text
                 Agent 专用程度高
                         ↑
     Dify              LangSmith
       \
        \          Orchestrator（目标位置）
         \                 |
应用定义 ──────────────────┼──────────────── 运行基础设施
                            |              Portainer
                            |       Temporal（横跨应用执行）
                         ↓
                 通用基础设施/执行
```

当前 Orchestrator 的实际位置仍更靠近“单机 OpenClaw 运维工具”，而非图中的目标位置。虽然已有 Runtime Node 心跳接收接口，但仓库还没有配套 Node Agent、远程实例管理或多节点调度；主要差距还包括异构 Runtime adapter、声明式状态收敛、统一事件/审计流和可扩展的健康探针。

## 当前实现核对

- 已有 PostgreSQL 持久化模型：`RuntimeNode`、`AgentInstance`、版本化配置、生命周期任务、任务事件、审计事件与 Warehouse outbox；任务支持幂等、lease、attempt fencing 和 retry schedule。
- 已有 OpenClaw 本机进程创建、独立 profile/端口/工作目录、启停、日志、WhatsApp 配对及诊断；消息和交易工作台属于领域应用，不是 Runtime Provider。
- 已有 Node Registry 发布制品读取、checksum/digest、Ed25519 验签、受限 Compose 生成、健康探测和单节点升级回滚。
- 已有 Runtime Node 心跳接收 API：按节点 ID 使用独立 Bearer 凭证写入 `runtime_nodes`，并可按心跳超时动态判断在线状态；本机 OpenClaw 实例持久化仍可创建本地节点记录。当前没有随仓库交付的心跳 Agent 客户端、节点控制台或远程 Runtime 生命周期操作，多节点部署还未完成。
- 当前 dashboard 只暴露 OpenClaw 本机 connector；Nanobot、Hermes、MCP Server 没有可操作的通用 adapter。产品不可宣称已经支持“任意 runtime 纳管”。
- 现有控制台仍使用钱包登录和 owner 隔离，尚无组织/项目/RBAC 管理后台、多租户管理员、密钥轮换界面或正式审计浏览器。
- 生命周期任务 worker 已支持安全的首期签名应用部署，但静态端口、多 Project 路由、配额、告警和备份恢复仍是上线前事项。

## 重构原则与阶段

1. **先统一领域和控制台**：Runtime Provider / Instance / desired-vs-observed 状态 / Capability 作为公共契约；业务应用工作台下沉到应用域。当前已开始通过 `/runtime/*` API 和实例控制台落实这一层。
2. **交付 Node Agent 闭环**：服务端已有 Runtime Node 基于节点级静态凭证的心跳和能力接收、在线超时判定；后续补凭证签发/轮换治理、受控客户端、任务 pull、事件上报和端到端联调。不得通过公共浏览器 API 直接远程执行 shell。
3. **adapter contract**：定义 `discover / create / start / stop / restart / inspect / logs / health / delete` 能力，逐个接入 OpenClaw、Nanobot；Hermes 需先确认其官方运行/配置协议。没有完整实现的 capability 要明确返回 unsupported。
4. **任务和审计闭环**：统一 OpenClaw 本机动作与 Node 安装任务的状态/事件模型；配置版本、权限、幂等、任务超时、重试、回滚、审计和告警策略保持一致。
5. **生产就绪**：RBAC/租户边界、密钥库、可观测性、配额和调度策略、灾备演练、迁移回滚、节点安全基线及多节点压力测试。

## 来源与置信度

- 产品职责：各产品官方文档，高置信；功能交叉部分按官方公开描述做归纳，中置信。
- 价格：各产品官方 pricing 页面，高置信为页面当前显示值；币种、套餐和用量计费会变，不视为长期价格承诺。
- 市场位置：产品类别定位，基于官方职责的分析判断，中置信；不是市场份额研究，也未覆盖所有 Agent 平台。
- 仓库现状：本仓库 API、SQLAlchemy 模型、worker、adapter 与测试，高置信。

官方价格来源：[Dify Cloud](https://dify.ai/pricing)、[LangSmith](https://www.langchain.com/pricing)、[Portainer](https://www.portainer.io/pricing)、[Temporal Cloud](https://temporal.io/pricing)。
