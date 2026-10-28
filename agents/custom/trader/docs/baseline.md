# Trader 当前基线

本文档用于沉淀 `agents/custom/trader` 的当前实现基线。它不是未来设计稿，而是对“今天仓库里已经落地的 Trader 智能体”做一次收口，方便后续继续演进时有明确参照。

## 1. 目标定位

当前 `Trader` 的定位是：

- 一个围绕持仓规则执行的交易智能体验证实现；
- 重点验证“规则化卖出执行”这条链路；
- 把交易判断收敛成可执行、可审计、可管理的智能体流程；
- 优先验证智能体运行边界、配置结构、控制面接入和审计沉淀；
- 暂时不追求真实券商实盘能力。

换句话说，当前版本更接近一个“持仓卖出执行智能体骨架 + MVP 策略执行链路”，而不是生产级交易系统，也不是完整的自动选股交易平台。

## 2. 当前范围

当前仓库里，Trader 已经覆盖的范围是：

1. 从同花顺 iFinD / QuantAPI 拉取市场数据；
2. 按策略配置逐个评估已关注标的；
3. 执行基础风控检查；
4. 生成订单意图；
5. 通过 broker 适配器执行模拟下单或 stub 下单；
6. 把状态、信号、订单和日志写入运行目录；
7. 被 Hub 后台读取并在前端展示。

当前最清楚、最可信、最应该被当成主路径的能力是：

1. 对已有持仓按规则执行卖出判断；
2. 在竞价、盘中回落、午后退出这些时点产出动作；
3. 把动作原因、状态变化和运行结果完整记录下来。

当前没有覆盖的范围是：

1. 东方财富真实实盘下单；
2. 可稳定使用的自动选股链路；
3. 板块/题材热度排序的真实市场结构建模；
4. 二浪/三浪的严格形态识别；
5. 盘口级别的“最后一档卖单消失”等触发逻辑；
6. 完整的买入触发链路；
7. 多账户、多券商、多市场的统一交易编排；
8. 生产级风控、确认流、告警流和回滚流。

## 3. 当前目录基线

```text
agents/custom/trader/
  config/
    trader.env.template
    strategies.yaml.template
  docs/
    baseline.md
    runbook.md
  scripts/
    bootstrap.sh
    run_once.sh
    start_bot.sh
    stop_bot.sh
    status_bot.sh
  src/
    audit/
    brokers/
    data/
    execution/
    risk/
    strategies/
    config.py
    main.py
    scheduler.py
    state_store.py
  README.md
  SKILL.md
  pyproject.toml
  uv.lock
```

目录职责可以简单理解为：

- `config/`：环境变量模板、策略配置模板；
- `docs/`：运行说明、基线文档；
- `scripts/`：本地操作入口；
- `src/data/`：数据源适配；
- `src/strategies/`：策略逻辑；
- `src/risk/`：风控规则；
- `src/execution/`：策略执行与订单规划；
- `src/brokers/`：下单适配器；
- `src/audit/`：审计日志相关；
- `src/main.py`：CLI 入口；
- `src/state_store.py`：状态持久化。

## 4. 运行模式基线

当前 Trader 支持两种 broker 模式：

- `paper`
  - 本地模拟成交；
  - 主要用于策略验证和工作台联调；
  - 不会发出真实订单。

- `eastmoney_stub`
  - 用来验证“券商边界”和“订单载荷”；
  - 只落盘，不触发真实券商执行；
  - 当前仍然属于安全验证模式。

结论很明确：**当前版本不会发送真实券商订单。**

## 5. 数据源基线

当前主数据源是同花顺 QuantAPI / iFinD：

- `IFIND_BASE_URL`
- `IFIND_ACCESS_TOKEN`
- `IFIND_REFRESH_TOKEN`
- `IFIND_REQUEST_TIMEOUT_MS`

这些配置来自：

- 模板文件：`config/trader.env.template`
- 实际本地文件：`config/trader.env`

当前默认调用链路是：

1. `src/main.py` 加载应用配置和策略配置；
2. 构造 `IfindClient`；
3. 逐个策略执行一个 cycle；
4. 将结果写入运行目录并输出日志。

## 6. 策略基线

当前仓库中已经落地的策略类型只有两类：

### 6.1 `breakout`

这是一个相对简单的区间突破示例策略，主要作用是：

- 验证最小策略配置结构；
- 验证数据读取与信号生成链路；
- 作为稳定的最小回归样本。

### 6.2 `auction_wave`

这是把 `SKILL.md` 中的“竞价 + 波段 + 午后退出”思路第一次落成常驻服务版本后的 MVP。

当前应明确视为“卖出侧是主路径、买入侧只是预留”的实现。

当前已明确落地的行为边界：

- 早盘 `09:29` 左右竞价区间判断；
- `< 0%`：直接卖出；
- `0% ~ 3%`：先卖一半，回到 `0%` 再卖剩余；
- `> 3%`：回落到 `3%` 以下时卖出；
- `14:30` 若未涨停则强制退出；
- 状态会写入运行目录，支持跨 cycle 持续判断。

当前尚未真正落地的能力：

- 稳定可用的买入主路径；
- 活跃板块 / 题材排序；
- 二浪 / 三浪形态严格判定；
- 强压力位更精细识别；
- 封板、盘口卖单消失等更细颗粒度触发；
- 真实东方财富下单。

## 7. 配置基线

当前配置分成两类：

### 7.1 环境配置

模板文件：`config/trader.env.template`

当前关键字段包括：

- `TRADER_LOOP_INTERVAL_SECONDS`
- `TRADER_LOG_LEVEL`
- `TRADER_RUNTIME_DIR`
- `TRADER_STRATEGY_FILE`
- `TRADER_BROKER`
- `IFIND_*`
- `TRADER_EASTMONEY_ACCOUNT_ID`

### 7.2 策略配置

模板文件：`config/strategies.yaml.template`

当前策略配置结构已经稳定到可以作为后续基线：

- `id`
- `enabled`
- `market`
- `symbol`
- `name`
- `timeframe`
- `strategy`
- `history_window`
- `breakout_lookback`
- `quantity`
- `position_quantity`
- `max_position`
- `stop_loss_pct`
- `take_profit_pct`
- `dry_run`

其中 `auction_wave` 还扩展了：

- `enable_buy`
- `lot_size`
- `active_lookback_days`
- `active_threshold_pct`
- `breakout_buffer_pct`
- `resistance_lookback_days`
- `resistance_buffer_pct`
- `afternoon_exit_time`
- `limit_up_threshold_pct`

## 8. 运行入口基线

当前本地操作入口已经固定为以下脚本：

```bash
./scripts/bootstrap.sh
./scripts/run_once.sh
./scripts/start_bot.sh
./scripts/stop_bot.sh
./scripts/status_bot.sh
```

它们的职责分别是：

- `bootstrap.sh`
  - 初始化 `trader.env` 和 `strategies.yaml`
  - 执行 `uv sync`

- `run_once.sh`
  - 执行单次 cycle
  - 适合测试、联调和事后分析

- `start_bot.sh`
  - 启动常驻服务

- `stop_bot.sh`
  - 停止常驻服务

- `status_bot.sh`
  - 查看进程与运行状态

## 9. 运行产物基线

当前运行状态会落到 `runtime/` 下，核心产物包括：

- `state.json`
  - 每个策略实例的持续状态

- `signals.jsonl`
  - 每次运行生成的信号记录

- `orders.jsonl`
  - 订单意图或 stub / paper 执行记录

- `logs/service.log`
  - 服务日志与每轮 cycle 结果

这些产物是当前 Hub 工作台读取 Trader 运行情况的基础数据来源之一。

## 10. Hub 接入基线

当前 Trader 已经接入 Hub，并形成了三层页面结构：

1. `/agents/trader`
   - 智能体首页
   - 看整体状态与策略列表

2. `/agents/trader/:strategyId`
   - 单策略页
   - 看状态、记录、配置

3. `/agents/trader/:strategyId/records/:recordId`
   - 单条记录详情页
   - 看记录摘要、字段、关联策略和原始 JSON

这意味着当前 Trader 已不只是 CLI 智能体，也已经具备一个最小可用的控制面展示路径。

## 11. 安全边界基线

当前必须明确的边界有：

1. 仓库中不提交真实凭证；
2. `config/trader.env` 仅用于本地；
3. 当前实现默认使用 `paper` 或 `eastmoney_stub`；
4. 当前实现不会触发真实东方财富订单；
5. 在同花顺接口额度有限的前提下，应优先复用 `runtime/` 中已有运行记录做分析，而不是重复请求外部接口。

## 12. 当前基线结论

截至当前版本，Trader 可以被定义为：

> 一个以同花顺数据为输入、以持仓卖出规则执行为主路径、以 paper/stub broker 为执行出口、以 Hub 为控制面展示入口的常驻式交易智能体 MVP。

它已经具备：

- 可运行的智能体目录结构；
- 可配置的策略清单；
- 可重复执行的单次 / 常驻运行入口；
- 基本可读的运行审计和状态持久化；
- Hub 中可用的列表、下钻和详情展示。

它暂时还不具备：

- 完整的自动选股买入能力；
- 真实实盘交易能力；
- 严格完成版的 `auction_wave` 策略；
- 完整的生产级风控与确认闭环。

后续所有 Trader 演进，都建议先判断一项改动属于下面哪一类：

1. 基线增强：在当前结构内补能力；
2. 模型升级：改变策略抽象或执行抽象；
3. 生产化改造：补真实券商、风控、审计、告警和鉴权闭环。
