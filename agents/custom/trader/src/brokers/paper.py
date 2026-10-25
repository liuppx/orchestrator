from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from .base import BrokerAdapter, OrderIntent


class PaperBroker(BrokerAdapter):
    provider = "paper"

    def broker_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "mode": "paper",
            "liveTradingEnabled": False,
            "configured": True,
        }

    def dry_run_order(self, order: OrderIntent) -> dict[str, Any]:
        notional = (order.latest_price or 0.0) * order.quantity
        return {
            "accepted": True,
            "provider": self.provider,
            "side": order.side,
            "symbol": order.symbol,
            "quantity": order.quantity,
            "latestPrice": order.latest_price,
            "estimatedNotional": round(notional, 4),
        }

    def place_order(self, order: OrderIntent) -> dict[str, Any]:
        preview = self.dry_run_order(order)
        preview.update(
            {
                "status": "filled",
                "filledAt": datetime.now(UTC).isoformat(),
                "orderId": f"paper-{order.strategy_id}-{int(datetime.now().timestamp())}",
            }
        )
        return preview
