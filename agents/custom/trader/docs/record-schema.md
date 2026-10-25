# Trader Runtime Record Schema

`runtime/runs/<run_id>/` 下的批次文件从 `trader.runtime_record.v1` 开始使用统一 snake_case 字段。

## 通用字段

所有批次记录都包含：

- `schema_version`
- `record_type`
- `run_id`
- `strategy_id`
- `strategy`
- `symbol`
- `stage`
- `ts`

## 记录类型

- `market_observation`
  - 写入 `market.json`
  - 记录数据源、请求窗口、最新价、昨收、观察时间和快照路径
- `market_observation_summary`
  - 写入 `market_observation.json`
  - 记录本轮候选池整体概览，包括 A/B/C 数量、错误数和交易许可
- `watchlist_candidate`
  - 写入 `watchlist_candidates.json`
  - 记录某个已配置观察标的的候选分级、分数、触发理由和风险提示
- `candidate_review_summary`
  - 写入 `candidate_review.json`
  - 记录本轮对上一轮候选池的复盘摘要，包括复盘数量、正收益数量、失败数量和平均收益
- `candidate_review`
  - 写入 `candidate_reviews.json`
  - 记录单个候选标的从上一轮入选价到本轮价格的简单收益和结果分类
- `signal`
  - 写入 `signals.json`
  - 记录动作、目标数量、风控结果、决策原因和策略 metadata
- `order_plan`
  - 写入 `order_plans.json`
  - 记录订单计划、风控结果和决策原因
- `order`
  - 写入 `orders.json`
  - 记录实际传给 broker 后得到的执行结果
- `suppressed_order`
  - 写入 `suppressed_orders.json`
  - 记录被订单防线抑制的重复交易意图
  - 当前主防线使用 `state.json` 里的 `order_ledgers.<YYYY-MM-DD>.orders`，按交易日记录已发送订单指纹
  - 兼容兜底仍会读取策略状态里的 `last_order_fingerprint`
  - `guard_reason` 目前包括 `duplicate_daily_order_ledger` 和 `duplicate_order_fingerprint`
- `error`
  - 写入 `errors.json`
  - 记录单个策略的行情、策略、风控或 broker 异常
  - 单个策略失败不会中断整轮运行

## 回放产物

`python -m src.main replay --run-id <run_id>` 会读取 `runtime/runs/<run_id>/market.json`，并把重算结果写入：

- `runtime/replays/<run_id>/<replay_id>/summary.json`
- `runtime/replays/<run_id>/<replay_id>/signals.json`
- `runtime/replays/<run_id>/<replay_id>/order_plans.json`
- `runtime/replays/<run_id>/<replay_id>/orders.json`

回放不会请求 iFinD，不会调用 broker，也不会更新 `runtime/state.json`。

## 状态账本

`runtime/state.json` 会维护按日订单账本：

- `order_ledgers.<YYYY-MM-DD>.orders.<strategy_id|symbol|side>`
  - `order_fingerprint`
  - `lifecycle_status`
    - `planned`：dry-run 或只形成待执行计划
    - `submitted`：已提交给 broker，但暂无最终成交状态
    - `filled`：broker 返回已成交
    - `partial`：broker 返回部分成交
    - `rejected`：broker 返回拒绝或失败
    - `canceled`：broker 返回取消
  - `broker_order_status`
  - `broker_order_id`
  - `broker_provider`
  - `accepted`
  - `guard_active`
  - `retry_allowed`
  - `guard_history`
  - `strategy_id`
  - `symbol`
  - `side`
  - `quantity`
  - `latest_price`
  - `run_id`
  - `submitted_at`
  - `filled_at`
  - `recorded_at`
  - `raw_result`

执行器只会在订单通过风控并发送到 broker 适配器后写入账本；被风控拦截或被重复订单防线抑制的订单不会刷新账本。

人工解锁不会删除账本记录，而是把 `guard_active` 置为 `false`、`retry_allowed` 置为 `true`，并追加 `guard_history`。下一轮同一订单意图允许重试；如果重试再次发送到 broker，执行器会把同一个账本 key 更新为新的激活记录，并保留历史解锁事件。

## 兼容说明

`runtime/signals.jsonl` 和 `runtime/orders.jsonl` 暂时保留以下兼容字段，供 Hub 旧读取逻辑继续使用：

- `strategyId`
- `latestPrice`
- `riskOk`
- `riskReason`

新代码应优先读取 snake_case 字段。后续 Hub 完成迁移后，可以移除滚动 JSONL 中的兼容别名。
