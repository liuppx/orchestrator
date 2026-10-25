from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OrderIntent:
    strategy_id: str
    symbol: str
    side: str
    quantity: int
    latest_price: float | None
    dry_run: bool
    metadata: dict[str, Any]


class BrokerAdapter:
    provider = "base"

    def broker_status(self) -> dict[str, Any]:
        raise NotImplementedError

    def dry_run_order(self, order: OrderIntent) -> dict[str, Any]:
        raise NotImplementedError

    def place_order(self, order: OrderIntent) -> dict[str, Any]:
        raise NotImplementedError
