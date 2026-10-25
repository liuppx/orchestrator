# Messenger

`agents/custom/messenger` 是当前仓库里用于承接多实例消息智能体的目录，对应控制台中的“信使”。

命名约定保持和现有 `trader -> 交易员` 一致：

- 目录名使用英文技术标识：`messenger`
- 控制台展示名使用中文角色名：`信使`

## 当前职责

当前 `messenger` 主要对应以下能力：

1. WhatsApp 多实例创建、启动、停止、删除
2. WhatsApp 配对二维码与运行日志查看
3. 钉钉实例创建与配置
4. 实例模型配置与基础运行状态管理

这些能力当前主要由仓库根目录的 `backend/` 控制面与 `frontend/` 控制台提供，尚未完全收口到 `agents/custom/messenger/` 目录中。

## 目录结构

```text
agents/custom/messenger/
  config/
  docs/
  scripts/
```

## 当前实现位置

目前相关实现主要分布在：

- `backend/src/hub/`
- `frontend/dist/index.html`

本目录当前承接的是智能体语义、配置约定和运维说明，不直接替代 hub 的现有实现。

## 约定文件

- [`config/messenger.env.template`](./config/messenger.env.template)
  用于约定信使相关环境变量
- [`docs/runbook.md`](./docs/runbook.md)
  用于记录当前控制方式、目录约定和后续收口方向
- [`scripts/README.md`](./scripts/README.md)
  用于说明脚本目录当前职责

## 后续建议

建议按下面的顺序继续演进：

1. 先把 hub 中与消息智能体强相关的说明同步到本目录
2. 再为 `messenger` 增加默认配置模板和运行脚本
3. 最后视需要把运行时配置、模板和脚本进一步收口到 `agents/custom/messenger/`
