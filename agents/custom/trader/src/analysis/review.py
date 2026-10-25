from __future__ import annotations

from typing import Any


def _as_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def build_candidate_reviews(
    *,
    run_id: str,
    previous_candidates: list[dict[str, Any]],
    current_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_by_key = {
        (str(item.get("strategy_id") or ""), str(item.get("symbol") or "")): item
        for item in current_candidates
        if isinstance(item, dict)
    }
    reviews: list[dict[str, Any]] = []
    for previous in previous_candidates:
        if not isinstance(previous, dict):
            continue
        key = (str(previous.get("strategy_id") or ""), str(previous.get("symbol") or ""))
        current = current_by_key.get(key)
        if not current:
            continue

        entry_price = _as_number(previous.get("latest_price"))
        review_price = _as_number(current.get("latest_price"))
        return_pct = None
        if entry_price not in (None, 0) and review_price is not None:
            return_pct = round((review_price - entry_price) / entry_price * 100, 4)

        if return_pct is None:
            outcome = "unknown"
        elif return_pct >= 3:
            outcome = "strong_follow_through"
        elif return_pct > 0:
            outcome = "positive"
        elif return_pct <= -3:
            outcome = "failed"
        else:
            outcome = "flat"

        reviews.append(
            {
                "schema_version": "trader.runtime_record.v1",
                "record_type": "candidate_review",
                "stage": "run_review",
                "run_id": run_id,
                "source_run_id": previous.get("run_id"),
                "strategy_id": key[0],
                "symbol": key[1],
                "name": previous.get("name") or key[0],
                "source_grade": previous.get("grade"),
                "source_score": previous.get("score"),
                "current_grade": current.get("grade"),
                "current_score": current.get("score"),
                "entry_price": entry_price,
                "review_price": review_price,
                "return_pct": return_pct,
                "outcome": outcome,
                "source_observed_at": _as_text(previous.get("observed_at")),
                "review_observed_at": _as_text(current.get("observed_at")),
                "source_signal_action": previous.get("signal_action"),
                "current_signal_action": current.get("signal_action"),
            }
        )
    return reviews


def build_candidate_review_summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    known_returns = [item["return_pct"] for item in reviews if isinstance(item.get("return_pct"), (int, float))]
    positive_count = sum(1 for value in known_returns if value > 0)
    failed_count = sum(1 for item in reviews if item.get("outcome") == "failed")
    average_return_pct = round(sum(known_returns) / len(known_returns), 4) if known_returns else None
    return {
        "schema_version": "trader.runtime_record.v1",
        "record_type": "candidate_review_summary",
        "stage": "run_review",
        "review_count": len(reviews),
        "positive_count": positive_count,
        "failed_count": failed_count,
        "average_return_pct": average_return_pct,
    }
