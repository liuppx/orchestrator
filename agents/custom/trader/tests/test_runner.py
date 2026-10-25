from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from src.brokers.base import BrokerAdapter, OrderIntent
from src.execution.ledger import unlock_order_guard
from src.execution.replay import replay_run
from src.execution.runner import run_cycle
from src.state_store import StateStore


class FakeIfindClient:
    def query_realtime_quotes(self, symbol: str) -> dict[str, Any]:
        return {
            "table": {
                "latest": [11.0],
                "preClose": [10.0],
            },
            "time": ["2026-07-01 10:00:00"],
        }

    def query_history_quotes(self, symbol: str, startdate: str, enddate: str) -> dict[str, Any]:
        return {
            "table": {
                "close": [9.5, 9.7, 9.8, 10.0, 10.2],
            }
        }


class PartiallyFailingIfindClient(FakeIfindClient):
    def query_realtime_quotes(self, symbol: str) -> dict[str, Any]:
        if symbol == "FAIL.SH":
            raise RuntimeError("ifind unavailable for FAIL.SH")
        return super().query_realtime_quotes(symbol)


class RisingIfindClient(FakeIfindClient):
    def query_realtime_quotes(self, symbol: str) -> dict[str, Any]:
        return {
            "table": {
                "latest": [12.0],
                "preClose": [11.0],
            },
            "time": ["2026-07-02 10:00:00"],
        }


class FakeBroker(BrokerAdapter):
    provider = "fake"

    def broker_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "mode": "test",
            "liveTradingEnabled": False,
            "configured": True,
        }

    def dry_run_order(self, order: OrderIntent) -> dict[str, Any]:
        return {
            "accepted": True,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
        }

    def place_order(self, order: OrderIntent) -> dict[str, Any]:
        return {
            "status": "filled",
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
        }


class CountingBroker(FakeBroker):
    def __init__(self) -> None:
        self.dry_run_calls = 0
        self.place_order_calls = 0

    def dry_run_order(self, order: OrderIntent) -> dict[str, Any]:
        self.dry_run_calls += 1
        return super().dry_run_order(order)

    def place_order(self, order: OrderIntent) -> dict[str, Any]:
        self.place_order_calls += 1
        return super().place_order(order)


class FilledBroker(CountingBroker):
    def place_order(self, order: OrderIntent) -> dict[str, Any]:
        self.place_order_calls += 1
        return {
            "accepted": True,
            "provider": self.provider,
            "status": "filled",
            "orderId": "filled-test-order-1",
            "filledAt": "2026-07-01T02:00:03+00:00",
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
        }


class NullLogger:
    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def exception(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeDsaBridge:
    def build_cycle_bundle(self, strategies: list[dict[str, Any]], run_day: date) -> dict[str, Any]:
        strategy = strategies[0]
        return {
            "provider": "dsa",
            "strategy_snapshots": {
                strategy["id"]: {
                    "symbol": strategy["symbol"],
                    "latest_price": 18.6,
                    "pre_close": 17.5,
                    "close_series": [15.2, 15.8, 16.5, 17.0, 17.5],
                    "observed_at": "2026-07-01 10:00:00",
                    "data_source": "dsa",
                    "quote_payload": {
                        "table": {
                            "latest": [18.6],
                            "preClose": [17.5],
                        },
                        "time": ["2026-07-01 10:00:00"],
                    },
                }
            },
            "market_overview": {
                "provider": "dsa",
                "market": "cn",
                "market_stats": {"up_count": 3200, "down_count": 1500},
                "hot_stocks": [{"code": "600519.SH", "name": "Kweichow Moutai"}],
                "warnings": [],
            },
            "alphasift_screen": {
                "strategy": "dual_low",
                "candidate_count": 1,
                "after_filter_count": 1,
                "llm_ranked": True,
                "warnings": [],
                "source_errors": [],
            },
            "watchlist_candidates": [
                {
                    "schema_version": "trader.runtime_record.v1",
                    "record_type": "watchlist_candidate",
                    "stage": "watchlist_candidate",
                    "strategy_id": "dsa-screen:dual_low",
                    "strategy": "dsa_alphasift",
                    "symbol": "600519.SH",
                    "name": "Kweichow Moutai",
                    "grade": "A",
                    "score": 88,
                    "trade_permission": "normal",
                    "latest_price": 18.6,
                    "pre_close": 17.5,
                    "pct_change": 6.2857,
                    "observed_at": "2026-07-01 10:00:00",
                    "signal_action": "observe",
                    "signal_reason": "generated by DSA AlphaSift",
                    "risk_ok": None,
                    "risk_reason": None,
                    "position": {},
                    "reasons": ["llm thesis"],
                    "risk_flags": [],
                }
            ],
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class RunCycleTest(unittest.TestCase):
    def test_run_cycle_uses_dsa_provider_bundle_for_market_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            result = run_cycle(
                strategies=[
                    {
                        "id": "breakout-test",
                        "enabled": True,
                        "symbol": "510300.SH",
                        "strategy": "breakout",
                        "breakout_lookback": 3,
                        "quantity": 100,
                        "max_position": 500,
                        "dry_run": True,
                    }
                ],
                ifind_client=FakeDsaBridge(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )

            run_dir = runtime_dir / "runs" / result["run_id"]
            summary = _read_json(run_dir / "summary.json")
            market_observation = _read_json(run_dir / "market_observation.json")
            watchlist_candidates = _read_json(run_dir / "watchlist_candidates.json")

            self.assertEqual(summary["market_observation"]["provider"], "dsa")
            self.assertEqual(market_observation["market_stats"]["up_count"], 3200)
            self.assertEqual(market_observation["alphasift_screening"]["strategy"], "dual_low")
            self.assertEqual(watchlist_candidates[0]["strategy_id"], "dsa-screen:dual_low")
            self.assertEqual(result["watchlist_candidate_count"], 2)

    def test_run_cycle_writes_run_batch_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            result = run_cycle(
                strategies=[
                    {
                        "id": "breakout-test",
                        "enabled": True,
                        "symbol": "510300.SH",
                        "strategy": "breakout",
                        "breakout_lookback": 3,
                        "quantity": 100,
                        "max_position": 500,
                        "dry_run": True,
                    }
                ],
                ifind_client=FakeIfindClient(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )

            run_id = result["run_id"]
            run_dir = runtime_dir / "runs" / run_id
            self.assertTrue(run_dir.is_dir())

            summary = _read_json(run_dir / "summary.json")
            market = _read_json(run_dir / "market.json")
            market_observation = _read_json(run_dir / "market_observation.json")
            watchlist_candidates = _read_json(run_dir / "watchlist_candidates.json")
            signals = _read_json(run_dir / "signals.json")
            orders = _read_json(run_dir / "orders.json")
            order_plans = _read_json(run_dir / "order_plans.json")

            self.assertEqual(summary["run_id"], run_id)
            self.assertEqual(summary["strategy_count"], 1)
            self.assertEqual(summary["watchlist_candidate_count"], 1)
            self.assertEqual(summary["market_observation"]["candidate_count"], 1)
            self.assertEqual(market[0]["run_id"], run_id)
            self.assertEqual(market[0]["schema_version"], "trader.runtime_record.v1")
            self.assertEqual(market[0]["record_type"], "market_observation")
            self.assertEqual(market[0]["stage"], "market_observation")
            self.assertEqual(market_observation["record_type"], "market_observation_summary")
            self.assertEqual(market_observation["candidate_count"], 1)
            self.assertEqual(market_observation["trade_permission"], "normal")
            self.assertEqual(watchlist_candidates[0]["record_type"], "watchlist_candidate")
            self.assertEqual(watchlist_candidates[0]["stage"], "watchlist_candidate")
            self.assertEqual(watchlist_candidates[0]["strategy_id"], "breakout-test")
            self.assertEqual(watchlist_candidates[0]["grade"], "A")
            self.assertEqual(watchlist_candidates[0]["trade_permission"], "normal")
            self.assertTrue(watchlist_candidates[0]["reasons"])
            self.assertEqual(signals[0]["run_id"], run_id)
            self.assertEqual(signals[0]["strategy_id"], "breakout-test")
            self.assertEqual(signals[0]["record_type"], "signal")
            self.assertTrue(signals[0]["decision_reason"])
            self.assertEqual(order_plans[0]["strategy_id"], "breakout-test")
            self.assertEqual(order_plans[0]["record_type"], "order_plan")
            self.assertEqual(orders[0]["run_id"], run_id)
            self.assertEqual(orders[0]["record_type"], "order")
            self.assertEqual(orders[0]["stage"], "order_execution")
            self.assertNotIn("strategyId", signals[0])
            self.assertNotIn("latestPrice", orders[0])

            state = _read_json(runtime_dir / "state.json")
            self.assertEqual(state["strategies"]["breakout-test"]["run_id"], run_id)
            self.assertEqual(state["strategies"]["breakout-test"]["runId"], run_id)

    def test_run_cycle_records_strategy_error_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            result = run_cycle(
                strategies=[
                    {
                        "id": "bad-strategy",
                        "enabled": True,
                        "symbol": "FAIL.SH",
                        "strategy": "breakout",
                        "breakout_lookback": 3,
                        "quantity": 100,
                        "max_position": 500,
                        "dry_run": True,
                    },
                    {
                        "id": "good-strategy",
                        "enabled": True,
                        "symbol": "510300.SH",
                        "strategy": "breakout",
                        "breakout_lookback": 3,
                        "quantity": 100,
                        "max_position": 500,
                        "dry_run": True,
                    },
                ],
                ifind_client=PartiallyFailingIfindClient(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )

            run_dir = runtime_dir / "runs" / result["run_id"]
            errors = _read_json(run_dir / "errors.json")
            signals = _read_json(run_dir / "signals.json")
            state = _read_json(runtime_dir / "state.json")

            self.assertEqual(result["strategy_count"], 2)
            self.assertEqual(result["error_count"], 1)
            self.assertEqual(errors[0]["strategy_id"], "bad-strategy")
            self.assertEqual(errors[0]["record_type"], "error")
            self.assertEqual(errors[0]["error_type"], "RuntimeError")
            self.assertEqual(signals[0]["strategy_id"], "good-strategy")
            self.assertEqual(state["strategies"]["bad-strategy"]["last_action"], "error")
            self.assertEqual(state["strategies"]["good-strategy"]["last_action"], "buy")

    def test_run_cycle_builds_candidate_review_from_previous_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            strategies = [
                {
                    "id": "breakout-test",
                    "enabled": True,
                    "symbol": "510300.SH",
                    "strategy": "breakout",
                    "breakout_lookback": 3,
                    "quantity": 100,
                    "max_position": 500,
                    "dry_run": True,
                }
            ]
            first = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )
            second = run_cycle(
                strategies=strategies,
                ifind_client=RisingIfindClient(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )

            second_dir = runtime_dir / "runs" / second["run_id"]
            reviews = _read_json(second_dir / "candidate_reviews.json")
            review_summary = _read_json(second_dir / "candidate_review.json")

            self.assertEqual(first["watchlist_candidate_count"], 1)
            self.assertEqual(second["candidate_review_count"], 1)
            self.assertEqual(reviews[0]["record_type"], "candidate_review")
            self.assertEqual(reviews[0]["strategy_id"], "breakout-test")
            self.assertEqual(reviews[0]["source_run_id"], first["run_id"])
            self.assertEqual(reviews[0]["return_pct"], 9.0909)
            self.assertEqual(reviews[0]["outcome"], "strong_follow_through")
            self.assertEqual(review_summary["record_type"], "candidate_review_summary")
            self.assertEqual(review_summary["review_count"], 1)
            self.assertEqual(review_summary["positive_count"], 1)

    def test_run_cycle_suppresses_duplicate_order_with_daily_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            state_store = StateStore(runtime_dir)
            broker = CountingBroker()
            strategies = [
                {
                    "id": "breakout-test",
                    "enabled": True,
                    "symbol": "510300.SH",
                    "strategy": "breakout",
                    "breakout_lookback": 3,
                    "quantity": 100,
                    "max_position": 500,
                    "dry_run": True,
                }
            ]

            first = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            second = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )

            first_dir = runtime_dir / "runs" / first["run_id"]
            second_dir = runtime_dir / "runs" / second["run_id"]
            first_orders = _read_json(first_dir / "orders.json")
            second_orders = _read_json(second_dir / "orders.json")
            second_order_plans = _read_json(second_dir / "order_plans.json")
            second_suppressed = _read_json(second_dir / "suppressed_orders.json")
            state = _read_json(runtime_dir / "state.json")

            self.assertEqual(first["order_count"], 1)
            self.assertEqual(first["suppressed_order_count"], 0)
            self.assertEqual(second["order_count"], 0)
            self.assertEqual(second["order_plan_count"], 0)
            self.assertEqual(second["suppressed_order_count"], 1)
            self.assertEqual(first_orders[0]["record_type"], "order")
            self.assertEqual(second_orders, [])
            self.assertEqual(second_order_plans, [])
            self.assertEqual(second_suppressed[0]["record_type"], "suppressed_order")
            self.assertEqual(second_suppressed[0]["guard_reason"], "duplicate_daily_order_ledger")
            self.assertEqual(second_suppressed[0]["trading_day"], date.today().isoformat())
            self.assertEqual(second_suppressed[0]["side"], "buy")
            self.assertEqual(broker.dry_run_calls, 1)
            self.assertEqual(broker.place_order_calls, 0)
            self.assertEqual(
                state["strategies"]["breakout-test"]["last_order_guard_reason"],
                "duplicate_daily_order_ledger",
            )
            self.assertEqual(
                state["strategies"]["breakout-test"]["order_result"]["mode"],
                "suppressed",
            )
            ledger = state["order_ledgers"][date.today().isoformat()]
            ledger_order = ledger["orders"]["breakout-test|510300.SH|buy"]
            self.assertEqual(ledger_order["lifecycle_status"], "planned")
            self.assertIsNone(ledger_order["broker_order_status"])
            self.assertIsNone(ledger_order["broker_order_id"])
            self.assertIsNone(ledger_order["submitted_at"])
            self.assertIsNone(ledger_order["filled_at"])
            self.assertEqual(ledger_order["raw_result"]["mode"], "dry_run")

    def test_run_cycle_records_filled_lifecycle_for_executed_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            state_store = StateStore(runtime_dir)
            broker = FilledBroker()

            result = run_cycle(
                strategies=[
                    {
                        "id": "breakout-test",
                        "enabled": True,
                        "symbol": "510300.SH",
                        "strategy": "breakout",
                        "breakout_lookback": 3,
                        "quantity": 100,
                        "max_position": 500,
                        "dry_run": False,
                    }
                ],
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            state = _read_json(runtime_dir / "state.json")
            ledger_order = state["order_ledgers"][date.today().isoformat()]["orders"]["breakout-test|510300.SH|buy"]

            self.assertEqual(result["order_count"], 1)
            self.assertEqual(broker.dry_run_calls, 1)
            self.assertEqual(broker.place_order_calls, 1)
            self.assertEqual(ledger_order["lifecycle_status"], "filled")
            self.assertEqual(ledger_order["broker_order_status"], "filled")
            self.assertEqual(ledger_order["broker_order_id"], "filled-test-order-1")
            self.assertEqual(ledger_order["submitted_at"], "2026-07-01T02:00:03+00:00")
            self.assertEqual(ledger_order["filled_at"], "2026-07-01T02:00:03+00:00")
            self.assertEqual(ledger_order["raw_result"]["mode"], "execute")

    def test_unlock_order_guard_allows_retry_without_deleting_audit_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            state_store = StateStore(runtime_dir)
            broker = CountingBroker()
            strategies = [
                {
                    "id": "breakout-test",
                    "enabled": True,
                    "symbol": "510300.SH",
                    "strategy": "breakout",
                    "breakout_lookback": 3,
                    "quantity": 100,
                    "max_position": 500,
                    "dry_run": True,
                }
            ]

            run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            unlock_result = unlock_order_guard(
                runtime_dir=runtime_dir,
                trading_day=date.today().isoformat(),
                strategy_id="breakout-test",
                symbol="510300.SH",
                side="buy",
                reason="manual retry after review",
                actor="unit-test",
            )
            second = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            second_dir = runtime_dir / "runs" / second["run_id"]
            state = _read_json(runtime_dir / "state.json")
            ledger_order = state["order_ledgers"][date.today().isoformat()]["orders"]["breakout-test|510300.SH|buy"]

            self.assertTrue(unlock_result["unlocked"])
            self.assertEqual(second["order_count"], 1)
            self.assertEqual(second["suppressed_order_count"], 0)
            self.assertEqual(_read_json(second_dir / "suppressed_orders.json"), [])
            self.assertEqual(broker.dry_run_calls, 2)
            self.assertTrue(ledger_order["guard_active"])
            self.assertFalse(ledger_order["retry_allowed"])
            self.assertEqual(ledger_order["lifecycle_status"], "planned")
            self.assertEqual(ledger_order["guard_history"][0]["event"], "order_guard_unlocked")
            self.assertEqual(ledger_order["guard_history"][0]["actor"], "unit-test")

    def test_run_cycle_keeps_legacy_last_fingerprint_duplicate_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            state_store = StateStore(runtime_dir)
            broker = CountingBroker()
            strategies = [
                {
                    "id": "breakout-test",
                    "enabled": True,
                    "symbol": "510300.SH",
                    "strategy": "breakout",
                    "breakout_lookback": 3,
                    "quantity": 100,
                    "max_position": 500,
                    "dry_run": True,
                }
            ]

            run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            state = state_store.load()
            state.pop("order_ledgers", None)
            state_store.save(state)

            second = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=broker,
                runtime_dir=runtime_dir,
                state_store=state_store,
                logger=NullLogger(),
            )
            second_dir = runtime_dir / "runs" / second["run_id"]
            second_suppressed = _read_json(second_dir / "suppressed_orders.json")

            self.assertEqual(second["order_count"], 0)
            self.assertEqual(second["order_plan_count"], 0)
            self.assertEqual(second["suppressed_order_count"], 1)
            self.assertEqual(second_suppressed[0]["guard_reason"], "duplicate_order_fingerprint")
            self.assertEqual(broker.dry_run_calls, 1)
            self.assertEqual(broker.place_order_calls, 0)

    def test_replay_run_uses_saved_market_snapshot_without_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            strategies = [
                {
                    "id": "breakout-test",
                    "enabled": True,
                    "symbol": "510300.SH",
                    "strategy": "breakout",
                    "breakout_lookback": 3,
                    "quantity": 100,
                    "max_position": 500,
                    "dry_run": True,
                }
            ]
            original = run_cycle(
                strategies=strategies,
                ifind_client=FakeIfindClient(),
                broker=FakeBroker(),
                runtime_dir=runtime_dir,
                state_store=StateStore(runtime_dir),
                logger=NullLogger(),
            )

            replay = replay_run(
                runtime_dir=runtime_dir,
                source_run_id=original["run_id"],
                strategies=strategies,
                logger=NullLogger(),
            )

            replay_dir = Path(replay["replay_dir"])
            signals = _read_json(replay_dir / "signals.json")
            order_plans = _read_json(replay_dir / "order_plans.json")
            orders = _read_json(replay_dir / "orders.json")

            self.assertEqual(replay["source_run_id"], original["run_id"])
            self.assertEqual(replay["order_count"], 0)
            self.assertEqual(signals[0]["source_run_id"], original["run_id"])
            self.assertEqual(signals[0]["action"], "buy")
            self.assertEqual(order_plans[0]["replay_note"], "replay only; no broker order was sent")
            self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
