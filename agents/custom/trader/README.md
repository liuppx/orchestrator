# Trader

`agents/custom/trader` 当前不是一个完整的“选股 + 买入 + 卖出 + 实盘”交易系统，但它的长期目标也不只是一个“卖出执行脚本”。

理解这个目录，建议分成两层：

1. **当前现实**
   - 它现在更接近一个围绕持仓规则执行的交易智能体；
   - 重点验证“按规则卖出”这条链路能否稳定跑通；
   - 把交易判断变成可执行、可审计、可管理的智能体流程。
2. **目标方向**
   - 它未来应逐步走向一个面向短线交易的决策智能体；
   - 持续朝交易 agent 方向迭代；
   - 重点能力应放在选股、买点、盘口确认和持仓退出。

当前已经落地的核心链路是：

1. 获取市场数据与候选池，
2. 对已配置策略逐个做规则判断，
3. 应用风控规则，
4. 生成订单意图，
5. 通过券商适配器做模拟执行或 stub 验证，
6. 持久化审计日志、运行状态和快照，
7. 由 Hub 后台读取并展示。

第一版实现刻意保持保守：

- 默认使用 `daily_stock_analysis` 作为市场观察、策略快照和候选池引擎；
- DSA 通过子进程桥接运行，和 Trader 主进程解耦；
- iFinD / QuantAPI 凭证暂时保留在环境变量里，作为备用数据源兼容层；
- 执行默认使用 `paper`；
- `eastmoney_stub` 只验证券商边界，不发送真实订单；
- 当前仓库内还不会发出任何真实券商订单。

## 当前现实

如果只用一句话描述当前版本，最贴切的是：

> 对持仓执行规则化卖出决策，并沉淀运行审计。

因此当前版本的主能力是：

- 按卖出规则执行单轮或常驻判断；
- 记录为什么卖、何时卖、卖多少；
- 在 Hub 中查看状态、记录和配置。

当前版本的次级能力是：

- 用 `breakout` 维持一个最小可回归样本；
- 用 `paper` / `eastmoney_stub` 验证 broker 边界；
- 为后续真实实盘接入做结构准备。

## 目标能力模型

如果只用一句话描述 Trader 应该走向哪里，更贴切的是：

> 一个面向短线交易的决策智能体，负责从市场热点中筛选候选标的，结合形态与盘口寻找买点，并对持仓执行规则化退出与审计管理。

围绕这个目标，核心能力应分成四层：

1. 市场层
   - 板块、题材、热度、情绪环境
2. 标的层
   - 候选池、形态、压力位、位置判断
3. 触发层
   - 买点、盘口、回封、卖点
4. 执行层
   - 风控、订单计划、审计、Hub 管理

当前代码主轴仍偏执行和卖出；未来真正要提升效果，主轴必须逐步转向选股、买点和盘口。

从 agent 视角看，Trader 后续要逐步形成：

- 观察市场
- 理解结构
- 筛选机会
- 生成计划
- 执行约束
- 审计复盘

## 当前策略范围

当前仓库里有两类策略：

- `breakout`：一个简单的区间突破示例策略。
- `auction_wave`：把 [`SKILL.md`](./SKILL.md) 中描述的思路，第一次落成常驻服务版本。

`auction_wave` 目前应当按一个完成度不对称的 MVP 来理解：

- 卖出侧规则大部分已经实现：
  - 在 `09:29` 左右锁定开盘竞价区间，
  - `< 0%`：全仓卖出，
  - `0% ~ 3%`：先卖一半，回落到 `0%` 再卖剩余仓位，
  - `> 3%`：盘中涨幅跌回 `3%` 以下时全卖，
  - `14:30` 未涨停时强制卖出。
- 买入侧规则仍然只是近似实现：
  - 近几日活跃度窗口，
  - 突破确认，
  - 粗粒度压力位规避。

`SKILL.md` 里的下列部分，当前还没有落成真正的市场结构逻辑：

- 活跃板块 / 题材筛选，
- 二浪 / 三浪识别，
- “最后一档卖单消失”这类盘口触发信号，
- 东方财富真实下单链路。

所以从产品理解上，当前 `Trader` 更像：

- 一个持仓卖出执行智能体，

而不是：

- 一个完整的自动选股买卖智能体。

## 文档入口

文档首页：

- [docs/index.md](./docs/index.md)
  - Trader 文档导航页

如果你要继续推进 Trader，建议按下面顺序看文档：

- [docs/baseline.md](./docs/baseline.md)
  - 当前基线，讲已经落地的现实能力
- [docs/capability-model.md](./docs/capability-model.md)
  - 目标能力模型，讲未来主轴为什么应放在选股、买点、盘口
- [docs/roadmap.md](./docs/roadmap.md)
  - 演进路线，讲先做什么后做什么
- [docs/run_once_sequence.puml](./docs/run_once_sequence.puml)
  - `run_once` 当前实际调用链路的 PlantUML 时序图
- [docs/runbook.md](./docs/runbook.md)
  - 本地运行手册

## 目录结构

```text
agents/custom/trader/
  config/
  docs/
  scripts/
  src/
```

## 快速开始

```bash
cd agents/custom/trader
./scripts/bootstrap.sh
python -m src.main init-config
python -m src.main diagnose
python -m src.main run-once
./scripts/start_bot.sh
./scripts/status_bot.sh
```

本地凭证放在 `config/trader.env`，该文件已加入 git ignore。
策略模板在 `config/strategies.yaml.template`；`init-config` 会在缺失时从模板生成本地 `config/strategies.yaml`，不会覆盖已有文件。

当前默认配置是 DSA 轻量模式，重点保证 `run-once` 能稳定产出：

- `market_observation.json`
- `watchlist_candidates.json`
- `summary.json`

该模式默认关闭较慢的扩展扫描项：

- `TRADER_DSA_ENABLE_MARKET_STATS`
- `TRADER_DSA_ENABLE_HOT_STOCKS`
- `TRADER_DSA_ENABLE_LIMIT_UP_POOL`
- `TRADER_DSA_ENABLE_SECTOR_RANKINGS`
- `TRADER_DSA_ENABLE_CONCEPT_RANKINGS`

如果这些开关保持 `false`，`market_observation.json` 里的 `provider_warnings` 会出现 `*_skipped` 标记。这是当前的预期行为，不代表运行失败。
