from __future__ import annotations

from pathlib import Path
from typing import Any

from ..audit.logger import utc_now_iso
from ..state_store import StateStore


def ledger_order_key(strategy_id: str, symbol: str, side: str) -> str:
    return "|".join([strategy_id, symbol, side])


def unlock_order_guard(
    *,
    runtime_dir: Path,
    trading_day: str,
    strategy_id: str,
    symbol: str,
    side: str,
    reason: str,
    actor: str = "manual",
) -> dict[str, Any]:
    state_store = StateStore(runtime_dir)
    state = state_store.load()
    ledgers = state.get("order_ledgers")
    if not isinstance(ledgers, dict):
        raise KeyError("order_ledgers not found in state")

    ledger = ledgers.get(trading_day)
    if not isinstance(ledger, dict):
        raise KeyError(f"order ledger not found for trading day: {trading_day}")

    orders = ledger.get("orders")
    if not isinstance(orders, dict):
        raise KeyError(f"orders not found for trading day: {trading_day}")

    key = ledger_order_key(strategy_id, symbol, side)
    entry = orders.get(key)
    if not isinstance(entry, dict):
        raise KeyError(f"ledger order not found: {key}")

    unlocked_at = utc_now_iso()
    audit_entry = {
        "event": "order_guard_unlocked",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "side": side,
        "reason": reason,
        "actor": actor,
        "ts": unlocked_at,
    }
    history = entry.setdefault("guard_history", [])
    if not isinstance(history, list):
        history = []
        entry["guard_history"] = history
    history.append(audit_entry)

    entry["guard_active"] = False
    entry["retry_allowed"] = True
    entry["unlocked_at"] = unlocked_at
    entry["unlocked_by"] = actor
    entry["unlock_reason"] = reason
    entry["previous_lifecycle_status"] = entry.get("lifecycle_status")
    entry["lifecycle_status"] = "unlocked"

    state_store.save(state)
    return {
        "unlocked": True,
        "trading_day": trading_day,
        "order_key": key,
        "entry": entry,
    }
