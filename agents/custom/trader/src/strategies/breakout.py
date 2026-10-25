from __future__ import annotations

from .base import MarketSnapshot, Signal, Strategy


class BreakoutStrategy(Strategy):
    def evaluate(self, snapshot: MarketSnapshot, prior_state: dict | None = None) -> Signal:
        closes = snapshot.close_series
        latest = snapshot.latest_price
        quantity = int(self.config.get("quantity", 100))
        lookback = int(self.config.get("breakout_lookback", 5))

        if latest is None:
            return Signal(
                action="hold",
                reason="missing latest price",
                target_quantity=0,
                latest_price=None,
                metadata={},
            )

        if len(closes) < lookback:
            return Signal(
                action="hold",
                reason=f"insufficient history, need {lookback} closes",
                target_quantity=0,
                latest_price=latest,
                metadata={"history_size": len(closes)},
            )

        recent = closes[-lookback:]
        breakout_high = max(recent)
        breakout_low = min(recent)
        if latest > breakout_high:
            return Signal(
                action="buy",
                reason=f"latest price {latest:.4f} broke above {lookback}-day high {breakout_high:.4f}",
                target_quantity=quantity,
                latest_price=latest,
                metadata={"breakout_high": breakout_high, "lookback": lookback},
            )
        if latest <= breakout_low:
            return Signal(
                action="sell",
                reason=f"latest price {latest:.4f} reached or fell below {lookback}-day low {breakout_low:.4f}",
                target_quantity=quantity,
                latest_price=latest,
                metadata={"breakout_low": breakout_low, "lookback": lookback},
            )
        return Signal(
            action="hold",
            reason=f"latest price {latest:.4f} remains inside {lookback}-day range",
            target_quantity=0,
            latest_price=latest,
            metadata={"breakout_high": breakout_high, "breakout_low": breakout_low, "lookback": lookback},
        )
