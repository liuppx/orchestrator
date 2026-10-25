# Hub Backend

`hub/backend` 是新的 Python 控制面骨架，目标定位是通用智能体管理后台。

当前迁移状态：

- 新的 Python 版本已经作为默认控制面入口
- 前端产品界面位于 `hub/frontend/`，构建产物输出到 `hub/frontend/dist/`

## 设计目标

控制面职责：

- 提供 hub API
- 发现并管理 `agents/` 下的智能体
- 统一起停、运行状态、配置读写和动作触发
- 通过适配器接入不同智能体，而不是在控制面里写死业务

## 当前骨架

```text
hub/backend/
  src/hub/
    api/
    adapters/
    models/
    services/
    app.py
    config.py
```

## 当前阶段

当前目录已经接管默认运行入口。

## 本地开发

本地配置：

```bash
cp hub/backend/.env.template hub/backend/.env
```

`.env` 只用于本地开发 backend，不提交到远端。

启动：

```bash
cd hub/backend
uv run python -m uvicorn hub.app:create_app --factory --reload --host 127.0.0.1 --port 3900
```

停止：在运行 backend 的终端里按 `Ctrl+C`。如果端口被旧进程占用，可执行：

```bash
lsof -tiTCP:3900 -sTCP:LISTEN | xargs kill
```

部署或打包后的启动、停止、重启统一使用仓库根目录下的 `scripts/starter.sh`。

## 测试

```bash
cd hub/backend
uv run python -m unittest discover -s tests
```

后续迁移建议：

1. 继续收敛控制面脚本和打包链路
2. 收敛智能体适配器边界，避免控制面里继续堆积智能体特例
3. 把钱包登录链路逐步升级到更完整的服务端校验模型
