from __future__ import annotations

from ..brokers.base import OrderIntent
from ..strategies.base import Signal


def build_order_intent(strategy_config: dict, signal: Signal) -> OrderIntent | None:
    if signal.action not in {"buy", "sell"}:
        return None
    return OrderIntent(
        strategy_id=strategy_config["id"],
        symbol=strategy_config["symbol"],
        side=signal.action,
        quantity=signal.target_quantity,
        latest_price=signal.latest_price,
        dry_run=bool(strategy_config.get("dry_run", True)),
        metadata=signal.metadata,
    )
