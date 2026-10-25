# Coder Bot

Feishu `issue` 协作智能体，负责把 GitHub issue 拉进飞书线程讨论，并按所选模型调用 `Codex` 或 `Claude` 执行修改、推分支、发 PR。

## 当前架构

- 飞书接入直接使用 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- 模型优先：用户只选模型，不再选执行器
- 后端内部自动推断：
  - `gpt-*` -> `codex`
  - `claude-*` -> `claude`
- 服务进程只有一个：`coder-bot`

## 快速开始

```bash
uv sync --frozen
cp config/coder-bot.env.template config/coder-bot.env
uv run --frozen coder-bot --env-file config/coder-bot.env doctor
./scripts/start_bot.sh
```

如果要安装 systemd 服务：

```bash
./scripts/bootstrap.sh
sudo systemctl status --no-pager coder-bot
```

## 飞书使用方式

1. 在 handoff 群发送 `/issue <repo>` 查看待处理 issue。
2. 发送 `/issue <repo> #<number> [model]` 进入该 issue 的讨论线程。
3. 在线程里继续讨论，必要时发送 `/model <name>` 切模型。
4. 发送 `/run` 或 `执行方案1` 开始执行。
5. 结果、进度和 PR 链接都会回到同一个线程。

示例：

```text
/issue deployer
/issue deployer #108 gpt-5.5
/model claude-opus-4-6
/run
```

## 相关文档

- [部署手册](docs/部署手册.md)
- [使用手册](docs/使用手册.md)
