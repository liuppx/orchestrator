from __future__ import annotations

from typing import Any


def evaluate_risk(strategy_config: dict[str, Any], signal_action: str, quantity: int) -> tuple[bool, str]:
    if signal_action == "hold":
        return True, "no order required"
    if quantity <= 0:
        return False, "target quantity must be positive"
    max_position = int(strategy_config.get("max_position", quantity))
    if quantity > max_position:
        return False, f"target quantity {quantity} exceeds max_position {max_position}"
    return True, "risk check passed"
