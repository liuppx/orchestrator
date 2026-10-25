# Trader 运行手册

## 目的

这个智能体是当前仓库里第一个常驻式交易服务验证实现。它不是最终的生产级交易系统。

## 模式

- `paper`：本地模拟成交，并写入审计记录。
- `eastmoney_stub`：验证券商请求载荷，并把“待执行订单”持久化下来，供人工复核。

## 命令

```bash
./scripts/bootstrap.sh
python -m src.main init-config
python -m src.main diagnose
python -m src.main run-once
./scripts/start_bot.sh
./scripts/stop_bot.sh
./scripts/status_bot.sh
```

`init-config` 只会创建缺失的 `config/trader.env` 和 `config/strategies.yaml`，不会覆盖已有本地配置。`diagnose` 会在启动前检查策略文件、market provider、broker、runtime 路径和策略摘要，输出中只显示凭证是否已配置，不打印 token 原文。

## DSA 主路径

当前推荐的可部署配置是：

- `TRADER_MARKET_PROVIDER=dsa`
- `TRADER_DSA_ROOT=/path/to/daily_stock_analysis`
- `TRADER_DSA_PYTHON_BIN=/path/to/daily_stock_analysis/.venv/bin/python`

DSA 通过独立 Python 环境运行。先准备 DSA 环境：

```bash
cd /Users/liuxin2/Workspace/opensource/daily_stock_analysis
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Trader 默认启用 DSA 轻量模式，以保证 `run-once` 能在可接受时间内完成。下面这些开关默认建议保持 `false`：

```bash
TRADER_DSA_ENABLE_MARKET_STATS=false
TRADER_DSA_ENABLE_HOT_STOCKS=false
TRADER_DSA_ENABLE_LIMIT_UP_POOL=false
TRADER_DSA_ENABLE_SECTOR_RANKINGS=false
TRADER_DSA_ENABLE_CONCEPT_RANKINGS=false
```

在这个模式下，候选池仍然会基于已配置策略标的生成；`market_observation.json` 中出现的 `*_skipped` 只是说明慢扫描项被主动关闭。

按历史批次回放：

```bash
python -m src.main replay --run-id <run_id>
```

回放只重算信号和订单计划，不请求外部行情源，不下单，也不更新 `state.json`。

人工解锁某条当日订单防线，允许下一轮相同订单意图重试：

```bash
python -m src.main unlock-order \
  --trading-day 2026-07-04 \
  --strategy-id breakout-test \
  --symbol 510300.SH \
  --side buy \
  --reason "manual retry after broker review" \
  --actor operator
```

解锁命令不会删除账本，只会把对应 ledger 记录标记为可重试，并写入 `guard_history`。

## 运行时文件

运行时状态会写入 `runtime/`：

- `state.json`
- `orders.jsonl`
- `signals.jsonl`
- `runs/<run_id>/summary.json`
- `runs/<run_id>/market.json`
- `runs/<run_id>/market_observation.json`
- `runs/<run_id>/watchlist_candidates.json`
- `runs/<run_id>/candidate_review.json`
- `runs/<run_id>/candidate_reviews.json`
- `runs/<run_id>/signals.json`
- `runs/<run_id>/orders.json`
- `runs/<run_id>/order_plans.json`
- `runs/<run_id>/suppressed_orders.json`
- `runs/<run_id>/errors.json`
- `logs/service.log`

批次文件使用统一 snake_case 记录结构，字段说明见 `docs/record-schema.md`。
滚动文件 `signals.jsonl` / `orders.jsonl` 暂时保留少量 camelCase 兼容字段，供 Hub 旧读取逻辑继续使用。

`market_observation.json` 和 `watchlist_candidates.json` 是当前第一版“市场观察 / 候选池”中间产物。默认 DSA 轻量模式下，它会至少覆盖已配置观察标的，做 A/B/C 分级和理由记录，用于人工判断和复盘，不构成自动买卖建议。

`candidate_review.json` 和 `candidate_reviews.json` 是当前第一版“候选池复盘”产物。它会把上一轮候选标的与本轮同标的行情做对比，计算简单收益和结果分布，帮助评估筛选质量，不代表真实交易绩效。

如果单个策略执行失败，错误会写入 `runs/<run_id>/errors.json`，整轮运行会继续处理其他启用策略。

如果某个策略连续产生完全相同的订单意图，执行器会优先用 `state.json` 里的当日 `order_ledgers` 抑制重复下单；被抑制的记录会写入 `runs/<run_id>/suppressed_orders.json`，本轮摘要会增加 `suppressed_order_count`。`last_order_fingerprint` 仍作为兼容兜底保留。

`order_ledgers` 也会记录订单生命周期：dry-run 订单标记为 `planned`；真实 broker 返回后会归一化为 `submitted`、`filled`、`partial`、`rejected` 或 `canceled`，并保留 broker 原始结果用于后续对账。

## 安全边界

- 当前实现不会发送任何真实券商订单。
- 凭证从 `config/trader.env` 加载。
- 如果策略配置缺失，服务会拒绝启动。

## 策略说明

当前 `auction_wave` 的行为是有意保守的：

- 会把每个策略实例的状态持久化到 `runtime/state.json`；
- 能表达早盘竞价卖出分支和下午强制退出；
- 卖出规则阈值已显式配置，包括 `auction_lock_time`、`auction_zero_threshold_pct`、`auction_strong_open_threshold_pct`、`afternoon_exit_time`、`limit_up_threshold_pct`；
- 每条信号的 `metadata.decision` 会记录统一动作枚举：`hold`、`buy`、`sell_half`、`sell_full`；
- 还没有真正的板级信号或盘口深度信号。

在把它视为生产级交易智能体之前，至少还需要补齐下面这些能力：

- 板块 / 题材活跃度排序，
- 波浪形态识别，
- 更严格的压力位判断，
- 盘中涨停与卖盘深度触发数据，
- 带显式确认和风控检查的真实券商适配器。
