from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestrator.adapters.trader import TraderAdapter


class TraderAdapterTest(unittest.TestCase):
    def build_root(self, root: Path) -> Path:
        trader_root = root / "agents" / "custom" / "trader"
        (trader_root / "config").mkdir(parents=True, exist_ok=True)
        return trader_root

    def write_strategy_file(self, trader_root: Path) -> Path:
        (trader_root / "config" / "trader.env").write_text(
            "TRADER_BROKER=paper\n"
            "IFIND_BASE_URL=https://quantapi.51ifind.com/api/v1\n"
            "IFIND_ACCESS_TOKEN=test-access-token\n"
            "IFIND_REFRESH_TOKEN=test-refresh-token\n",
            encoding="utf-8",
        )
        strategy_path = trader_root / "config" / "strategies.yaml"
        strategy_path.write_text(
            yaml.safe_dump(
                {
                    "strategies": [
                        {
                            "id": "etf-breakout-demo",
                            "symbol": "510300.SH",
                            "name": "CSI300 ETF Demo",
                            "strategy": "breakout",
                            "enabled": True,
                        },
                        {
                            "id": "auction-wave-demo",
                            "symbol": "159915.SZ",
                            "name": "Auction Wave Demo",
                            "strategy": "auction_wave",
                            "enabled": False,
                        },
                    ]
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return strategy_path

    def write_runtime_files(self, trader_root: Path) -> None:
        runtime_dir = trader_root / "runtime" / "logs"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (trader_root / "runtime" / "state.json").write_text(
            json.dumps(
                {
                    "strategies": {
                        "etf-breakout-demo": {
                            "symbol": "510300.SH",
                            "strategy": "breakout",
                            "lastAction": "hold",
                            "lastReason": "inside range",
                            "latestPrice": 4.907,
                            "observedAt": "2026-06-27T16:01:24",
                            "riskOk": True,
                            "riskReason": "no order required",
                            "snapshotPath": "/tmp/snapshot.jsonl",
                            "strategyState": {},
                            "metadata": {},
                            "orderResult": None,
                        }
                    },
                    "order_ledgers": {
                        "2026-07-01": {
                            "orders": {
                                "etf-breakout-demo|510300.SH|buy": {
                                    "order_fingerprint": "fp-1",
                                    "strategy_id": "etf-breakout-demo",
                                    "symbol": "510300.SH",
                                    "side": "buy",
                                    "quantity": 100,
                                    "latest_price": 4.9,
                                    "lifecycle_status": "planned",
                                    "guard_active": True,
                                    "retry_allowed": False,
                                    "recorded_at": "2026-07-01T02:00:01+00:00",
                                }
                            }
                        }
                    },
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        (trader_root / "runtime" / "signals.jsonl").write_text(
            '{"ts":"2026-06-27T08:29:59.616885+00:00","strategyId":"etf-breakout-demo","symbol":"510300.SH","action":"hold"}\n',
            encoding="utf-8",
        )
        (trader_root / "runtime" / "orders.jsonl").write_text("", encoding="utf-8")
        (trader_root / "runtime" / "logs" / "service.log").write_text(
            '2026-06-27 16:29:59,618 INFO cycle result={"brokerStatus":{"provider":"paper"},"strategyCount":1,"results":[{"symbol":"510300.SH"}]}\n',
            encoding="utf-8",
        )
        run_dir = trader_root / "runtime" / "runs" / "20260701T020000Z-test0001"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "summary.json").write_text(
            '{"run_id":"20260701T020000Z-test0001","run_started_at":"2026-07-01T02:00:00+00:00","run_finished_at":"2026-07-01T02:00:02+00:00","strategy_count":1,"signal_count":1,"order_plan_count":0,"order_count":0,"suppressed_order_count":2,"error_count":1,"watchlist_candidate_count":1}',
            encoding="utf-8",
        )
        (run_dir / "market_observation.json").write_text(
            '{"record_type":"market_observation_summary","candidate_count":1,"trade_permission":"watch_only","grade_counts":{"A":0,"B":1,"C":0}}',
            encoding="utf-8",
        )
        (run_dir / "watchlist_candidates.json").write_text(
            '[{"record_type":"watchlist_candidate","strategy_id":"etf-breakout-demo","symbol":"510300.SH","grade":"B","score":48,"trade_permission":"watch_only","reasons":["near range high"],"risk_flags":[]}]',
            encoding="utf-8",
        )
        (run_dir / "candidate_review.json").write_text(
            '{"record_type":"candidate_review_summary","review_count":1,"positive_count":1,"failed_count":0,"average_return_pct":2.5}',
            encoding="utf-8",
        )
        (run_dir / "candidate_reviews.json").write_text(
            '[{"record_type":"candidate_review","strategy_id":"etf-breakout-demo","symbol":"510300.SH","return_pct":2.5,"outcome":"positive"}]',
            encoding="utf-8",
        )

    def test_update_config_preserves_other_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            strategy_path = self.write_strategy_file(trader_root)
            adapter = TraderAdapter(trader_root)

            result = adapter.update_config(
                "paper",
                {
                    "id": "auction-wave-demo",
                    "symbol": "510300.SH",
                    "name": "Auction Wave Demo Updated",
                    "strategy": "auction_wave",
                    "enabled": True,
                },
                "auction-wave-demo",
            )

            self.assertTrue(result.saved)
            self.assertEqual(result.strategyCount, 2)
            loaded = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))
            strategies = loaded["strategies"]
            self.assertEqual(len(strategies), 2)
            self.assertEqual(strategies[0]["id"], "etf-breakout-demo")
            self.assertEqual(strategies[1]["id"], "auction-wave-demo")
            self.assertEqual(strategies[1]["name"], "Auction Wave Demo Updated")
            self.assertTrue(strategies[1]["enabled"])

    def test_summary_uses_latest_run_with_analysis_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            self.write_strategy_file(trader_root)
            self.write_runtime_files(trader_root)
            runs = trader_root / "runtime" / "runs"
            newer = runs / "20260702T020000Z-service-cycle"
            newer.mkdir()
            (newer / "summary.json").write_text(
                '{"run_id":"20260702T020000Z-service-cycle","run_finished_at":"2026-07-02T02:00:02+00:00"}',
                encoding="utf-8",
            )
            summary = TraderAdapter(trader_root).summary()
            self.assertEqual(summary.recent_runs[0]["run_id"], "20260702T020000Z-service-cycle")
            self.assertEqual(summary.market_observation["candidate_count"], 1)
            self.assertEqual(summary.watchlist_candidates[0]["symbol"], "510300.SH")

    def test_update_config_can_match_old_strategy_id_when_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            strategy_path = self.write_strategy_file(trader_root)
            adapter = TraderAdapter(trader_root)

            result = adapter.update_config(
                "eastmoney_stub",
                {
                    "id": "auction-wave-live",
                    "symbol": "159915.SZ",
                    "name": "Auction Wave Live",
                    "strategy": "auction_wave",
                    "enabled": True,
                },
                "auction-wave-demo",
            )

            self.assertTrue(result.saved)
            self.assertEqual(result.broker, "eastmoney_stub")
            loaded = yaml.safe_load(strategy_path.read_text(encoding="utf-8"))
            strategy_ids = [item["id"] for item in loaded["strategies"]]
            self.assertEqual(strategy_ids, ["etf-breakout-demo", "auction-wave-live"])

    def test_summary_exposes_last_cycle_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            self.write_strategy_file(trader_root)
            self.write_runtime_files(trader_root)
            adapter = TraderAdapter(trader_root)

            summary = adapter.summary()

            self.assertEqual(summary.last_cycle_strategy_count, 1)
            self.assertEqual(summary.last_cycle_request_count, 2)
            self.assertEqual(summary.last_snapshot_path, "/tmp/snapshot.jsonl")
            self.assertEqual(len(summary.recent_runs), 1)
            self.assertEqual(summary.recent_runs[0]["run_id"], "20260701T020000Z-test0001")
            self.assertEqual(summary.recent_runs[0]["strategy_count"], 1)
            self.assertEqual(summary.recent_runs[0]["suppressed_order_count"], 2)
            self.assertEqual(summary.recent_runs[0]["error_count"], 1)
            self.assertEqual(summary.market_observation["candidate_count"], 1)
            self.assertEqual(summary.market_observation["trade_permission"], "watch_only")
            self.assertEqual(len(summary.watchlist_candidates), 1)
            self.assertEqual(summary.watchlist_candidates[0]["grade"], "B")
            self.assertEqual(summary.watchlist_candidates[0]["strategy_id"], "etf-breakout-demo")
            self.assertEqual(summary.candidate_review["review_count"], 1)
            self.assertEqual(summary.candidate_review["average_return_pct"], 2.5)
            self.assertEqual(len(summary.candidate_reviews), 1)
            self.assertEqual(summary.candidate_reviews[0]["outcome"], "positive")
            self.assertEqual(len(summary.order_ledger_entries), 1)
            self.assertEqual(summary.order_ledger_entries[0]["trading_day"], "2026-07-01")
            self.assertEqual(summary.order_ledger_entries[0]["order_key"], "etf-breakout-demo|510300.SH|buy")
            self.assertTrue(summary.order_ledger_entries[0]["guard_active"])
            self.assertTrue(summary.diagnostics["ok"])
            self.assertTrue(summary.diagnostics["data_source"]["access_token_configured"])
            self.assertEqual(summary.diagnostics["strategies"]["enabled_count"], 1)

    def test_diagnose_action_returns_sanitized_runtime_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            self.write_strategy_file(trader_root)
            adapter = TraderAdapter(trader_root)

            result = adapter.run_action("diagnose")
            payload = json.loads(result.stdout)

            self.assertTrue(result.executed)
            self.assertEqual(result.action, "diagnose")
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["data_source"]["access_token_configured"])
            self.assertNotIn("test-access-token", result.stdout)
            self.assertNotIn("test-refresh-token", result.stdout)

    def test_init_config_action_creates_missing_files_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            (trader_root / "config" / "trader.env.template").write_text("TRADER_BROKER=paper\n", encoding="utf-8")
            (trader_root / "config" / "strategies.yaml.template").write_text("strategies: []\n", encoding="utf-8")
            (trader_root / "config" / "trader.env").write_text("TRADER_BROKER=eastmoney_stub\n", encoding="utf-8")
            adapter = TraderAdapter(trader_root)

            result = adapter.run_action("init_config")
            payload = json.loads(result.stdout)

            self.assertTrue(result.executed)
            self.assertEqual(result.action, "init_config")
            self.assertTrue(payload["ok"])
            self.assertEqual((trader_root / "config" / "trader.env").read_text(encoding="utf-8"), "TRADER_BROKER=eastmoney_stub\n")
            self.assertEqual((trader_root / "config" / "strategies.yaml").read_text(encoding="utf-8"), "strategies: []\n")
            statuses = {item["kind"]: item["status"] for item in payload["files"]}
            self.assertEqual(statuses["env"], "exists")
            self.assertEqual(statuses["strategy"], "created")

    def test_unlock_order_guard_marks_ledger_entry_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trader_root = self.build_root(Path(tmpdir))
            self.write_strategy_file(trader_root)
            self.write_runtime_files(trader_root)
            adapter = TraderAdapter(trader_root)

            result = adapter.unlock_order_guard(
                {
                    "trading_day": "2026-07-01",
                    "strategy_id": "etf-breakout-demo",
                    "symbol": "510300.SH",
                    "side": "buy",
                    "reason": "manual retry from hub",
                    "actor": "unit-test",
                }
            )
            state = json.loads((trader_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            entry = state["order_ledgers"]["2026-07-01"]["orders"]["etf-breakout-demo|510300.SH|buy"]

            self.assertTrue(result.executed)
            self.assertEqual(result.action, "unlock_order")
            self.assertFalse(entry["guard_active"])
            self.assertTrue(entry["retry_allowed"])
            self.assertEqual(entry["lifecycle_status"], "unlocked")
            self.assertEqual(entry["guard_history"][0]["actor"], "unit-test")


if __name__ == "__main__":
    unittest.main()
