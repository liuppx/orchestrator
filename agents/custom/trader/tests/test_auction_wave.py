from __future__ import annotations

import unittest
from datetime import datetime

from src.strategies.auction_wave import AuctionWaveStrategy
from src.strategies.base import MarketSnapshot


def _snapshot(latest: float, observed_at: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="510300.SH",
        latest_price=latest,
        pre_close=10.0,
        close_series=[9.5, 9.7, 9.9, 10.0, 10.1],
        observed_at=datetime.strptime(observed_at, "%Y-%m-%d %H:%M:%S"),
    )


class AuctionWaveSellRuleTest(unittest.TestCase):
    def test_zero_to_three_open_sells_half_and_records_decision(self) -> None:
        strategy = AuctionWaveStrategy(
            {
                "position_quantity": 200,
                "lot_size": 100,
                "auction_lock_time": "09:29",
                "auction_open_end_time": "09:35",
                "auction_zero_threshold_pct": 0.0,
                "auction_strong_open_threshold_pct": 3.0,
            }
        )

        signal = strategy.evaluate(_snapshot(10.2, "2026-07-01 09:31:00"), {})

        self.assertEqual(signal.action, "sell")
        self.assertEqual(signal.target_quantity, 100)
        self.assertEqual(signal.metadata["auction_bucket"], "zero_to_three")
        self.assertEqual(signal.metadata["decision"]["action_type"], "sell_half")
        self.assertEqual(signal.metadata["decision"]["rule_id"], "auction_zero_to_three_sell_half")
        self.assertTrue(signal.metadata["next_state"]["morning_half_sold"])
        self.assertEqual(signal.metadata["next_state"]["remaining_quantity"], 100)

    def test_above_three_retrace_uses_configured_threshold(self) -> None:
        strategy = AuctionWaveStrategy(
            {
                "position_quantity": 200,
                "auction_strong_open_threshold_pct": 4.0,
                "auction_zero_threshold_pct": 0.0,
            }
        )
        prior_state = {
            "remaining_quantity": 200,
            "initial_quantity": 200,
            "position_opened": True,
            "auction_bucket": "above_three",
        }

        signal = strategy.evaluate(_snapshot(10.35, "2026-07-01 10:00:00"), prior_state)

        self.assertEqual(signal.action, "sell")
        self.assertEqual(signal.target_quantity, 200)
        self.assertEqual(signal.metadata["decision"]["action_type"], "sell_full")
        self.assertEqual(signal.metadata["decision"]["rule_id"], "above_three_retrace_full_exit")
        self.assertEqual(signal.metadata["sell_rules"]["auction_strong_open_threshold_pct"], 4.0)
        self.assertFalse(signal.metadata["next_state"]["position_opened"])


if __name__ == "__main__":
    unittest.main()
