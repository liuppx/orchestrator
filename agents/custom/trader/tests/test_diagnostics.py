from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from src.config import AppConfig
from src.diagnostics import diagnose_config
from src.main import build_broker, init_config_once


def _config(app_dir: Path, **overrides: object) -> AppConfig:
    values = {
        "app_dir": app_dir,
        "env_file": app_dir / "config" / "trader.env",
        "runtime_dir": app_dir / "runtime",
        "strategy_file": app_dir / "config" / "strategies.yaml",
        "loop_interval_seconds": 300,
        "log_level": "INFO",
        "broker": "paper",
        "market_provider": "ifind",
        "ifind_base_url": "https://quantapi.51ifind.com/api/v1",
        "ifind_access_token": "secret-access-token",
        "ifind_refresh_token": "secret-refresh-token",
        "ifind_request_timeout_ms": 30000,
        "eastmoney_account_id": None,
        "dsa_root": app_dir / "daily_stock_analysis",
        "dsa_python_bin": "python",
        "dsa_market": "cn",
        "dsa_use_alphasift": True,
        "dsa_timeout_seconds": 120,
        "dsa_screening_strategy": "dual_low",
        "dsa_screening_max_results": 12,
        "dsa_hot_stock_count": 10,
        "dsa_limit_up_count": 12,
        "dsa_concept_top_n": 5,
        "dsa_sector_top_n": 5,
        "dsa_history_days": 40,
        "dsa_enable_market_stats": False,
        "dsa_enable_hot_stocks": False,
        "dsa_enable_limit_up_pool": False,
        "dsa_enable_sector_rankings": False,
        "dsa_enable_concept_rankings": False,
    }
    values.update(overrides)
    return AppConfig(**values)


class DiagnosticsTest(unittest.TestCase):
    def write_strategy_file(self, app_dir: Path) -> None:
        (app_dir / "config").mkdir(parents=True, exist_ok=True)
        (app_dir / "config" / "strategies.yaml").write_text(
            "strategies:\n"
            "  - id: breakout-test\n"
            "    symbol: 510300.SH\n"
            "    strategy: breakout\n"
            "    enabled: true\n"
            "  - id: auction-test\n"
            "    symbol: 159915.SZ\n"
            "    strategy: auction_wave\n"
            "    enabled: false\n",
            encoding="utf-8",
        )

    def test_diagnose_config_reports_ready_without_leaking_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            self.write_strategy_file(app_dir)
            result = diagnose_config(
                _config(app_dir),
                broker_status={
                    "provider": "paper",
                    "configured": True,
                    "accessToken": "broker-secret-token",
                },
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["strategies"]["count"], 2)
            self.assertEqual(result["strategies"]["enabled_count"], 1)
            self.assertTrue(result["data_source"]["access_token_configured"])
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("secret-access-token", serialized)
            self.assertNotIn("secret-refresh-token", serialized)
            self.assertNotIn("broker-secret-token", serialized)
            self.assertTrue(result["broker"]["status"]["accessTokenConfigured"])

    def test_diagnose_config_reports_blocking_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            result = diagnose_config(
                _config(
                    app_dir,
                    broker="unknown",
                    ifind_access_token=None,
                    ifind_refresh_token=None,
                )
            )

            self.assertFalse(result["ok"])
            self.assertIn("unsupported broker: unknown", result["errors"])
            self.assertTrue(any("strategy file not found" in item for item in result["errors"]))
            self.assertIn("IFIND_ACCESS_TOKEN or IFIND_REFRESH_TOKEN is required for ifind provider", result["errors"])

    def test_diagnose_config_reports_dsa_bridge_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            self.write_strategy_file(app_dir)
            result = diagnose_config(
                _config(
                    app_dir,
                    market_provider="dsa",
                    ifind_access_token=None,
                    ifind_refresh_token=None,
                    dsa_root=app_dir / "missing-dsa",
                )
            )

            self.assertFalse(result["ok"])
            self.assertIn(f"DSA root not found: {app_dir / 'missing-dsa'}", result["errors"])

    def test_build_broker_rejects_unknown_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unsupported broker"):
                build_broker(_config(Path(tmp), broker="unknown"))

    def test_eastmoney_stub_status_is_not_configured_without_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broker = build_broker(_config(Path(tmp), broker="eastmoney_stub", eastmoney_account_id=None))
            status = broker.broker_status()

            self.assertFalse(status["configured"])
            self.assertFalse(status["accountIdConfigured"])

    def test_init_config_creates_missing_files_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            (app_dir / "config").mkdir(parents=True, exist_ok=True)
            (app_dir / "config" / "trader.env.template").write_text("TRADER_BROKER=paper\n", encoding="utf-8")
            (app_dir / "config" / "strategies.example.yaml").write_text("strategies: []\n", encoding="utf-8")
            (app_dir / "config" / "trader.env").write_text("TRADER_BROKER=eastmoney_stub\n", encoding="utf-8")

            with redirect_stdout(StringIO()):
                result = init_config_once(_config(app_dir))

            self.assertTrue(result["ok"])
            self.assertEqual((app_dir / "config" / "trader.env").read_text(encoding="utf-8"), "TRADER_BROKER=eastmoney_stub\n")
            self.assertEqual((app_dir / "config" / "strategies.yaml").read_text(encoding="utf-8"), "strategies: []\n")
            statuses = {item["kind"]: item["status"] for item in result["files"]}
            self.assertEqual(statuses["env"], "exists")
            self.assertEqual(statuses["strategy"], "created")


if __name__ == "__main__":
    unittest.main()
