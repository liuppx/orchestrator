# 配置说明

更新时间：2026-10-28

## Orchestrator 服务

Orchestrator 后端只有一个服务配置来源：`backend/.env`。模板是 [`backend/.env.template`](../backend/.env.template)，首次运行复制一次：

```bash
cp backend/.env.template backend/.env
```

本地和生产使用同一文件路径，通过不同的值区分环境。后端启动和迁移从该文件读取服务配置。不要在仓库根目录或 `config/` 目录再维护一份 Hub/OpenClaw 服务 env；根层旧配置已移除。模板内按用途解释了监听、PostgreSQL、运行目录、Router、钱包会话、Runtime Node、管理员/Project/worker 凭证和部署 worker 参数。

## 独立 Agent 配置

下列模板分别属于独立进程，不是 Orchestrator 后端的第二套配置。按各自 README 和脚本复制到同目录的本地文件；凭证只写入被 Git 忽略的本地文件。

| 模板 | 用途与目标文件 |
| --- | --- |
| [`agents/openclaw/issuer/config/github-app.config.env.template`](../agents/openclaw/issuer/config/github-app.config.env.template) | Issuer 的 GitHub App 与 WebDAV 凭证；`bootstrap.sh` 复制为 `config/github-app.config.env` |
| [`agents/openclaw/issuer/config/openclaw.json.template`](../agents/openclaw/issuer/config/openclaw.json.template) | Issuer 专属 OpenClaw/飞书配置；复制为 `config/openclaw.json`。它包含飞书应用、群白名单、Router 和 Gateway 配置，不是 Orchestrator 的服务 env |
| [`agents/openclaw/issuer/config/policy.json.template`](../agents/openclaw/issuer/config/policy.json.template) | Issuer 的管理员、确认/取消命令、仓库别名和 GitHub 用户别名；复制为 `config/policy.json` |
| [`agents/openclaw/coder/config/coder-bot.env.template`](../agents/openclaw/coder/config/coder-bot.env.template) | Coder Bot 独立服务配置；按 Coder README 复制为 `config/coder-bot.env` |
| [`agents/custom/trader/config/trader.env.template`](../agents/custom/trader/config/trader.env.template) | Trader 的运行参数和行情数据源凭证；复制为 `config/trader.env` |
| [`agents/custom/trader/config/strategies.yaml.template`](../agents/custom/trader/config/strategies.yaml.template) | Trader 策略样例；初始化为 `config/strategies.yaml`，默认保持 `dry_run: true` |
| [`ops/army/army.env.template`](../ops/army/army.env.template) | Army 运维 worker 配置；启动脚本复制为忽略的 `ops/army/army.env` |

`agents/custom/messenger/config/messenger.env.template` 当前是未来 Messenger 独立运行时的配置约定，现有 Orchestrator 和 Messenger 代码不会读取它；不要将它当作服务启动配置。

## 严格 JSON 模板

Issuer 的 `openclaw.json.template` 和 `policy.json.template` 必须保持严格 JSON：初始化脚本及运行时代码直接解析 JSON，不能在文件中加入 `//` 或 `/* */` 注释。字段说明集中维护在这里：

- OpenClaw `models.providers.router` 设置模型服务 URL、API key、协议及模型名；`agents.defaults` 指定主模型、工作目录和上下文压缩策略。
- `messages.groupChat` 控制群聊可见回复与触发规则；`commands` 控制原生命令、技能及重启命令。
- `channels.feishu` 保存飞书 App ID/Secret、WebSocket 模式、群白名单、是否必须 @、历史条数和媒体大小限制。生产环境必须替换所有 `replace_with_*` 值，并只允许预期的群。
- `gateway` 控制本机网关端口、模式和访问 token；token 应使用随机值，文件权限需限制为服务账号可读。
- `plugins` 只启用本配置需要的飞书插件。
- policy `admins` 是可执行管理/确认操作的飞书用户 ID 列表；`confirmCommands` 和 `cancelCommands` 定义提交及取消指令；`repoAliases` 把短名映射为 GitHub owner/repository；`githubUserAliases` 把业务跟进人名称映射为 GitHub login。

仓库中所有真正可复制的配置或内容模板使用 `.template` 结尾。`.env.template`、`.json.template` 和 `.yaml.template` 的实际目标格式由它们的完整文件名决定；不要为了统一后缀而改变目标文件格式。
