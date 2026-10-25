from __future__ import annotations

from typing import Any

from ..strategies.base import MarketSnapshot, Signal


def _pct_change(latest: float | None, pre_close: float | None) -> float | None:
    if latest is None or pre_close in (None, 0):
        return None
    return round((latest - pre_close) / pre_close * 100, 4)


def _breakout_position(snapshot: MarketSnapshot, lookback: int) -> dict[str, Any]:
    closes = snapshot.close_series[-lookback:] if lookback > 0 else snapshot.close_series
    if not closes or snapshot.latest_price is None:
        return {
            "lookback": lookback,
            "high": None,
            "low": None,
            "range_position": None,
            "above_high_pct": None,
        }

    high = max(closes)
    low = min(closes)
    span = high - low
    range_position = None if span == 0 else round((snapshot.latest_price - low) / span, 4)
    above_high_pct = None if high == 0 else round((snapshot.latest_price - high) / high * 100, 4)
    return {
        "lookback": lookback,
        "high": high,
        "low": low,
        "range_position": range_position,
        "above_high_pct": above_high_pct,
    }


def build_watchlist_candidate(
    *,
    run_id: str,
    strategy_config: dict[str, Any],
    snapshot: MarketSnapshot,
    signal: Signal,
    risk_ok: bool,
    risk_reason: str,
) -> dict[str, Any]:
    pct = _pct_change(snapshot.latest_price, snapshot.pre_close)
    lookback = int(strategy_config.get("breakout_lookback", strategy_config.get("history_window", 5)) or 5)
    position = _breakout_position(snapshot, lookback)
    reasons: list[str] = []
    risk_flags: list[str] = []
    score = 0

    if pct is None:
        risk_flags.append("missing intraday change")
    elif pct >= 3:
        score += 30
        reasons.append(f"intraday change is strong at {pct:.2f}%")
    elif pct >= 1:
        score += 18
        reasons.append(f"intraday change is positive at {pct:.2f}%")
    elif pct <= -2:
        score -= 20
        risk_flags.append(f"intraday change is weak at {pct:.2f}%")
    else:
        score += 6
        reasons.append(f"intraday change is modest at {pct:.2f}%")

    above_high_pct = position.get("above_high_pct")
    range_position = position.get("range_position")
    if isinstance(above_high_pct, (int, float)) and above_high_pct > 0:
        score += 28
        reasons.append(f"latest price is {above_high_pct:.2f}% above lookback high")
    elif isinstance(range_position, (int, float)) and range_position >= 0.8:
        score += 14
        reasons.append("latest price is near the top of its lookback range")
    elif isinstance(range_position, (int, float)) and range_position <= 0.2:
        score -= 12
        risk_flags.append("latest price is near the bottom of its lookback range")

    if signal.action == "buy":
        score += 30
        reasons.append("strategy produced an entry signal")
    elif signal.action == "sell":
        score -= 25
        risk_flags.append("strategy produced an exit signal")
    else:
        reasons.append("strategy is observing without an order")

    if not risk_ok:
        score -= 20
        risk_flags.append(risk_reason)

    if score >= 65:
        grade = "A"
        trade_permission = "normal"
    elif score >= 35:
        grade = "B"
        trade_permission = "watch_only"
    else:
        grade = "C"
        trade_permission = "avoid"

    return {
        "schema_version": "trader.runtime_record.v1",
        "record_type": "watchlist_candidate",
        "stage": "watchlist_candidate",
        "run_id": run_id,
        "strategy_id": strategy_config["id"],
        "strategy": strategy_config.get("strategy", "breakout"),
        "symbol": snapshot.symbol,
        "name": strategy_config.get("name") or strategy_config["id"],
        "grade": grade,
        "score": max(0, min(100, score)),
        "trade_permission": trade_permission,
        "latest_price": snapshot.latest_price,
        "pre_close": snapshot.pre_close,
        "pct_change": pct,
        "observed_at": snapshot.observed_at.isoformat(timespec="seconds") if snapshot.observed_at else None,
        "signal_action": signal.action,
        "signal_reason": signal.reason,
        "risk_ok": risk_ok,
        "risk_reason": risk_reason,
        "position": position,
        "reasons": reasons,
        "risk_flags": risk_flags,
    }


def build_market_observation_summary(candidates: list[dict[str, Any]], error_count: int) -> dict[str, Any]:
    grade_counts = {
        "A": sum(1 for item in candidates if item.get("grade") == "A"),
        "B": sum(1 for item in candidates if item.get("grade") == "B"),
        "C": sum(1 for item in candidates if item.get("grade") == "C"),
    }
    if grade_counts["A"] > 0:
        trade_permission = "normal"
    elif grade_counts["B"] > 0:
        trade_permission = "watch_only"
    else:
        trade_permission = "avoid"

    top_candidates = sorted(candidates, key=lambda item: int(item.get("score") or 0), reverse=True)[:5]
    return {
        "schema_version": "trader.runtime_record.v1",
        "record_type": "market_observation_summary",
        "stage": "market_observation",
        "candidate_count": len(candidates),
        "grade_counts": grade_counts,
        "error_count": error_count,
        "trade_permission": trade_permission,
        "top_candidates": [
            {
                "strategy_id": item.get("strategy_id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "grade": item.get("grade"),
                "score": item.get("score"),
                "trade_permission": item.get("trade_permission"),
                "signal_action": item.get("signal_action"),
            }
            for item in top_candidates
        ],
    }
