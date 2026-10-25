from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class MarketSnapshot:
    symbol: str
    latest_price: float | None
    pre_close: float | None
    close_series: list[float]
    observed_at: datetime | None


@dataclass
class Signal:
    action: str
    reason: str
    target_quantity: int
    latest_price: float | None
    metadata: dict[str, Any]


class Strategy:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def evaluate(self, snapshot: MarketSnapshot, prior_state: dict[str, Any] | None = None) -> Signal:
        raise NotImplementedError
