# Issuer

基于 OpenClaw + 飞书 + GitHub App 的 issue 协作服务。

当前这套实现面向群聊协作，重点不是“单人提单”，而是“群里起草、群里确认、群里继续改”。

## 核心能力

- `@issuer` 创建或更新 issue 时，先生成草案，再由 `/confirm <id>` 真正写入 GitHub。
- 同群成员可以通过 `/show <id>`、`/edit <id> <要求>`、`/confirm <id>`、`/cancel <id>` 协作处理同一份草案。
- `/show all` 会列出当前群里所有待处理草案，只显示 `pending` 草案。
- `/issue <repo> [limit]` 直接列出最近 open issues。
- `/close <repo> #<number>` 直接关闭 issue，不走草案。
- `/assignees <repo> #<number> <who>` 直接追加指派人，不替换已有 assignees。
- 创建、更新、`/edit` 改写时会保留正文里的 `跟进人：姓名`；如果 `config/policy.json` 配了 `githubUserAliases`，会自动同步 GitHub assignee。
- 飞书图片和文件会在执行阶段上传到外部存储，并回写到 GitHub 正文 / 评论。

## 目录结构

- 源代码：`workspace_assets/`
- 核心模块：`workspace_assets/tools/pending_action.mjs`、`workspace_assets/tools/github_issue_create.mjs`、`workspace_assets/tools/github_issue_update.mjs`、`workspace_assets/tools/github_issue_close.mjs`、`workspace_assets/tools/github_issue_comment.mjs`、`workspace_assets/hooks/confirmation-bridge/handler.ts`
- 配置文件：`config/`
- 运维脚本：`scripts/`
- 运行数据：`data/`
- 文档：`docs/`

## 快速开始

首次或升级后建议先做一次初始化和同步：

```bash
bash scripts/bootstrap.sh
bash scripts/sync_workspace.sh
```

手动启动：

```bash
./scripts/start_gateway.sh
./scripts/status_gateway.sh
```

如果你刚改过 `workspace_assets/`：

```bash
./scripts/sync_workspace.sh
./scripts/stop_gateway.sh
./scripts/start_gateway.sh
```

systemd 部署或升级：

```bash
BOT_USER="$(id -un)" ./scripts/install_systemd.sh
sudo systemctl restart issuer-openclaw-gateway
sudo systemctl status --no-pager issuer-openclaw-gateway
```

## 飞书侧常用用法

- 创建 issue：`@issuer 在 agent 创建 issue，标题是“登录接口超时”，内容是“补充重试和告警”`
- 更新 issue：`@issuer 修改 agent #123，把标题改成“登录接口偶发超时”，正文补充复现步骤`
- 查看草案：`/show abcd`
- 查看当前群全部草案：`/show all`
- 整份改写草案：`/edit abcd 跟进人改成刘鑫，正文补上复现步骤`
- 补充草案正文：`abcd 补充：增加期望结果`
- 提交草案：`/confirm abcd`
- 取消草案：`/cancel abcd`
- 查看最近 issues：`/issue agent 10`
- 直接关闭：`/close agent #123`
- 直接追加指派人：`/assignees agent #123 刘鑫`

建议在群里统一使用显式草案 ID，不要依赖旧的 `draft:` 前缀写法。

## 常用排障

- 看 gateway 状态和最近日志：

```bash
./scripts/status_gateway.sh
tail -f data/logs/openclaw-gateway.log
```

- 看当前群 / 当前仓库的 pending 草案：

```bash
./scripts/inspect_pending.sh summary
./scripts/inspect_pending.sh list
./scripts/inspect_pending.sh list --repo yeying-community/agent
./scripts/inspect_pending.sh show --draft-id 5d496c27
```

- 看命令是否命中 `confirmation-bridge`：

```bash
tail -f data/logs/issuer-audit.jsonl
```

如果你刚改过 `workspace_assets/`，但群里效果没变，通常不是模型问题，而是运行时 workspace 没同步：

```bash
./scripts/sync_workspace.sh
./scripts/stop_gateway.sh
./scripts/start_gateway.sh
```

## 文档

- [使用手册](docs/使用手册.md)
- [部署手册](docs/部署手册.md)

## 附件上传

附件在执行 `github_issue_create.mjs`、`github_issue_update.mjs`、`github_issue_comment.mjs` 时会自动从最新飞书会话中识别，并上传到外部附件存储。

附件默认上传到 WebDAV。在 `config/github-app.config.env` 中至少配置：

```bash
WEBDAV_BASE_URL=https://webdav.your-domain.example/dav/personal/issue-pictures
WEBDAV_USERNAME=your-key-id
WEBDAV_PASSWORD=your-key-secret
WEBDAV_PUBLIC_SHARE_API_URL=https://webdav.your-domain.example/api/v1/public/share/create
WEBDAV_PUBLIC_SHARE_BEARER_TOKEN=your-bearer-token
```

如果你的服务方使用 `Key ID / Key Secret` 命名，也可以改用：

```bash
WEBDAV_KEY_ID=your-key-id
WEBDAV_KEY_SECRET=your-key-secret
```

上传成功后：

- 会先调用公开分享接口生成外链
- 图片会在 GitHub Issue/Comment 中以内联 Markdown 形式展示
- 非图片文件会显示为“查看文件”链接
- 远端目录按 `issuer-attachments/YYYY/MM/DD/<timestamp>-<random>-filename` 自动分层
