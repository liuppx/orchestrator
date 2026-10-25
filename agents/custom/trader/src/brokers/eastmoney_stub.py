from __future__ import annotations

from datetime import datetime, UTC

from .base import BrokerAdapter, OrderIntent


class EastMoneyStubBroker(BrokerAdapter):
    provider = "eastmoney_stub"

    def __init__(self, account_id: str | None) -> None:
        self.account_id = account_id

    def broker_status(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "mode": "stub",
            "configured": bool(self.account_id),
            "liveTradingEnabled": False,
            "accountIdConfigured": bool(self.account_id),
            "note": "Stub adapter only validates payloads and records pending execution plans.",
        }

    def dry_run_order(self, order: OrderIntent) -> dict[str, object]:
        return {
            "accepted": True,
            "provider": self.provider,
            "reviewRequired": True,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "latestPrice": order.latest_price,
        }

    def place_order(self, order: OrderIntent) -> dict[str, object]:
        return {
            "accepted": True,
            "provider": self.provider,
            "status": "pending_manual_execution",
            "reviewRequired": True,
            "queuedAt": datetime.now(UTC).isoformat(),
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "latestPrice": order.latest_price,
        }
