from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


def _load_payload(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload must be an object")
    return data


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def _value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    if hasattr(value, key):
        return getattr(value, key)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        if isinstance(payload, dict):
            return payload.get(key)
    return None


def _canonical_symbol(symbol: str) -> str:
    raw = _text(symbol).upper()
    if not raw:
        return raw
    if raw.endswith((".SH", ".SZ", ".BJ", ".HK", ".KS", ".KQ", ".TW", ".TWO", ".T")):
        return raw
    if raw.startswith("HK") and raw[2:].isdigit():
        return f"{raw[2:].zfill(5)}.HK"
    if raw.isdigit() and len(raw) == 5:
        return f"{raw}.HK"
    if raw.isdigit() and len(raw) == 6:
        if raw.startswith(("43", "83", "87", "88", "92")):
            return f"{raw}.BJ"
        if raw.startswith(("5", "6", "9")):
            return f"{raw}.SH"
        return f"{raw}.SZ"
    return raw


def _symbol_lookup_keys(symbol: str) -> set[str]:
    canonical = _canonical_symbol(symbol)
    keys = {canonical}
    upper = canonical.upper()
    if "." in upper:
        base, suffix = upper.split(".", 1)
        keys.add(base)
        keys.add(f"{suffix}:{base}")
    if upper.startswith("HK") and upper[2:].isdigit():
        keys.add(upper[2:])
        keys.add(f"HK:{upper[2:]}")
    return {item for item in keys if item}


def _pct_change(latest_price: float | None, pre_close: float | None) -> float | None:
    if latest_price is None or pre_close in (None, 0):
        return None
    return round((latest_price - pre_close) / pre_close * 100, 4)


def _range_position(latest_price: float | None, closes: list[float]) -> tuple[float | None, float | None]:
    if latest_price is None or not closes:
        return None, None
    high = max(closes)
    low = min(closes)
    if high == low:
        return 0.5, 0.0
    position = (latest_price - low) / (high - low)
    above_high_pct = (latest_price - high) / high * 100 if high else None
    return round(position, 4), round(above_high_pct, 4) if above_high_pct is not None else None


def _grade_from_score(score: float) -> tuple[str, str]:
    if score >= 75:
        return "A", "normal"
    if score >= 45:
        return "B", "watch_only"
    return "C", "avoid"


def _normalize_alphasift_candidate(
    candidate: dict[str, Any],
    *,
    strategy_id: str,
    symbol_to_strategy: dict[str, str],
) -> dict[str, Any]:
    raw_symbol = _text(candidate.get("code") or candidate.get("symbol"))
    symbol = _canonical_symbol(raw_symbol)
    mapped_strategy = next((strategy for key, strategy in symbol_to_strategy.items() if key in _symbol_lookup_keys(symbol)), None)
    score = _float(candidate.get("score"))
    if score is None:
        llm_score = _float(candidate.get("llm_score"))
        score = llm_score if llm_score is not None else 50.0
    grade, trade_permission = _grade_from_score(score)
    latest_price = _float(candidate.get("price") or candidate.get("latest_price"))
    pct_change = _float(candidate.get("change_pct") or candidate.get("pct_change"))
    reasons: list[str] = []
    for key in ("reason", "llm_thesis"):
        text = _text(candidate.get(key))
        if text:
            reasons.append(text)
    catalysts = candidate.get("llm_catalysts")
    if isinstance(catalysts, list):
        reasons.extend(_text(item) for item in catalysts if _text(item))
    risk_flags: list[str] = []
    risk_level = _text(candidate.get("risk_level"))
    if risk_level:
        risk_flags.append(f"risk level: {risk_level}")
    for key in ("risk_flags", "llm_risks"):
        value = candidate.get(key)
        if isinstance(value, list):
            risk_flags.extend(_text(item) for item in value if _text(item))
        elif _text(value):
            risk_flags.append(_text(value))
    return {
        "schema_version": "trader.runtime_record.v1",
        "record_type": "watchlist_candidate",
        "stage": "watchlist_candidate",
        "run_id": "",
        "strategy_id": mapped_strategy or strategy_id,
        "strategy": "dsa_alphasift",
        "symbol": symbol,
        "name": _text(candidate.get("name")) or symbol,
        "grade": grade,
        "score": max(0, min(100, round(score, 2))),
        "trade_permission": trade_permission,
        "latest_price": latest_price,
        "pre_close": None,
        "pct_change": pct_change,
        "observed_at": None,
        "signal_action": "observe",
        "signal_reason": _text(candidate.get("reason")) or "generated by DSA AlphaSift",
        "risk_ok": None,
        "risk_reason": None,
        "position": {
            "industry": _text(candidate.get("industry")),
            "amount": _float(candidate.get("amount")),
            "llm_confidence": _float(candidate.get("llm_confidence")),
        },
        "reasons": [item for item in reasons if item],
        "risk_flags": [item for item in risk_flags if item],
        "source_metadata": {
            "provider": "dsa_alphasift",
            "raw": candidate,
        },
    }


def _normalize_market_candidate(
    *,
    quote: Any,
    closes: list[float],
    strategy_id: str,
    source_tags: list[str],
) -> dict[str, Any]:
    symbol = _canonical_symbol(_text(_value(quote, "stock_code") or _value(quote, "code")))
    latest_price = _float(_value(quote, "current_price") or _value(quote, "price"))
    pre_close = _float(_value(quote, "prev_close") or _value(quote, "pre_close"))
    pct_change = _float(_value(quote, "change_percent") or _value(quote, "change_pct"))
    if pct_change is None:
        pct_change = _pct_change(latest_price, pre_close)
    range_position, above_high_pct = _range_position(latest_price, closes[-20:])
    score = 20.0
    reasons: list[str] = []
    risk_flags: list[str] = []

    if pct_change is not None:
        if pct_change >= 5:
            score += 28
            reasons.append(f"price momentum is strong at {pct_change:.2f}%")
        elif pct_change >= 2:
            score += 18
            reasons.append(f"price is up {pct_change:.2f}%")
        elif pct_change <= -3:
            score -= 18
            risk_flags.append(f"price is weak at {pct_change:.2f}%")

    if range_position is not None:
        if range_position >= 0.8:
            score += 16
            reasons.append("price is near the top of its 20-day range")
        elif range_position <= 0.2:
            score -= 10
            risk_flags.append("price is near the bottom of its 20-day range")

    if above_high_pct is not None and above_high_pct > 0:
        score += 14
        reasons.append(f"price is {above_high_pct:.2f}% above recent high")

    if "hot_stock" in source_tags:
        score += 12
        reasons.append("appears in DSA hot-stock feed")
    if "limit_up_pool" in source_tags:
        score += 16
        reasons.append("appears in DSA limit-up pool")

    grade, trade_permission = _grade_from_score(score)
    return {
        "schema_version": "trader.runtime_record.v1",
        "record_type": "watchlist_candidate",
        "stage": "watchlist_candidate",
        "run_id": "",
        "strategy_id": strategy_id,
        "strategy": "dsa_market",
        "symbol": symbol,
        "name": _text(_value(quote, "stock_name") or _value(quote, "name")) or symbol,
        "grade": grade,
        "score": max(0, min(100, round(score, 2))),
        "trade_permission": trade_permission,
        "latest_price": latest_price,
        "pre_close": pre_close,
        "pct_change": pct_change,
        "observed_at": _text(_value(quote, "update_time") or _value(quote, "provider_timestamp") or _value(quote, "fetched_at")),
        "signal_action": "observe",
        "signal_reason": "generated by DSA market scanners",
        "risk_ok": None,
        "risk_reason": None,
        "position": {
            "range_position": range_position,
            "above_high_pct": above_high_pct,
            "source_tags": source_tags,
        },
        "reasons": reasons,
        "risk_flags": risk_flags,
        "source_metadata": {
            "provider": "dsa_market",
            "source_tags": source_tags,
        },
    }


def _prepare_environment(dsa_root: Path, use_alphasift: bool) -> None:
    sys.path.insert(0, str(dsa_root))
    os.chdir(dsa_root)
    if use_alphasift:
        os.environ["ALPHASIFT_ENABLED"] = "true"
    from src.config import setup_env

    setup_env()


def _build_strategy_snapshot(
    manager: Any,
    strategy: dict[str, Any],
    *,
    history_days: int,
) -> dict[str, Any]:
    symbol = _text(strategy.get("symbol"))
    quote = manager.get_realtime_quote(symbol)
    history = manager.get_daily_data(symbol, days=history_days)
    history_frame = history[0] if isinstance(history, tuple) and history else history
    closes: list[float] = []
    if hasattr(history_frame, "iterrows"):
        for _, row in history_frame.iterrows():
            close_value = _float(row.get("close"))
            if close_value is not None:
                closes.append(close_value)
    observed_at = _text(_value(quote, "update_time") or _value(quote, "provider_timestamp") or _value(quote, "fetched_at"))
    return {
        "symbol": _canonical_symbol(symbol),
        "latest_price": _float(_value(quote, "current_price") or _value(quote, "price")),
        "pre_close": _float(_value(quote, "prev_close") or _value(quote, "pre_close")),
        "close_series": closes,
        "observed_at": observed_at,
        "data_source": "dsa",
        "quote_payload": _plain(quote),
        "history_summary": {
            "row_count": len(closes),
        },
    }


def _manager_market_overview(manager: Any, payload: dict[str, Any]) -> dict[str, Any]:
    sector_top_n = _int(payload.get("sector_top_n"), 5)
    concept_top_n = _int(payload.get("concept_top_n"), 5)
    hot_stock_count = _int(payload.get("hot_stock_count"), 10)
    limit_up_count = _int(payload.get("limit_up_count"), 12)
    enable_market_stats = bool(payload.get("enable_market_stats"))
    enable_hot_stocks = bool(payload.get("enable_hot_stocks"))
    enable_limit_up_pool = bool(payload.get("enable_limit_up_pool"))
    enable_sector_rankings = bool(payload.get("enable_sector_rankings"))
    enable_concept_rankings = bool(payload.get("enable_concept_rankings"))
    overview: dict[str, Any] = {
        "provider": "dsa",
        "market": _text(payload.get("market")) or "cn",
        "warnings": [],
        "enabled_features": {
            "market_stats": enable_market_stats,
            "hot_stocks": enable_hot_stocks,
            "limit_up_pool": enable_limit_up_pool,
            "sector_rankings": enable_sector_rankings,
            "concept_rankings": enable_concept_rankings,
        },
    }
    if enable_market_stats:
        try:
            overview["market_stats"] = _plain(manager.get_market_stats(purpose="trader"))
        except Exception as exc:  # noqa: BLE001
            overview.setdefault("warnings", []).append(f"market_stats_failed: {exc}")
    else:
        overview.setdefault("warnings", []).append("market_stats_skipped")
    if enable_hot_stocks:
        try:
            overview["hot_stocks"] = _plain(manager.get_hot_stocks(n=hot_stock_count))
        except Exception as exc:  # noqa: BLE001
            overview.setdefault("warnings", []).append(f"hot_stocks_failed: {exc}")
            overview["hot_stocks"] = []
    else:
        overview["hot_stocks"] = []
        overview.setdefault("warnings", []).append("hot_stocks_skipped")
    if enable_limit_up_pool:
        try:
            overview["limit_up_pool"] = _plain(manager.get_limit_up_pool(n=limit_up_count))
        except Exception as exc:  # noqa: BLE001
            overview.setdefault("warnings", []).append(f"limit_up_pool_failed: {exc}")
            overview["limit_up_pool"] = []
    else:
        overview["limit_up_pool"] = []
        overview.setdefault("warnings", []).append("limit_up_pool_skipped")
    if enable_sector_rankings:
        try:
            lead, lag = manager.get_sector_rankings(n=sector_top_n) or ([], [])
            overview["sector_rankings"] = {"leaders": _plain(lead), "laggards": _plain(lag)}
        except Exception as exc:  # noqa: BLE001
            overview.setdefault("warnings", []).append(f"sector_rankings_failed: {exc}")
    else:
        overview.setdefault("warnings", []).append("sector_rankings_skipped")
    if enable_concept_rankings:
        try:
            lead, lag = manager.get_concept_rankings(n=concept_top_n) or ([], [])
            overview["concept_rankings"] = {"leaders": _plain(lead), "laggards": _plain(lag)}
        except Exception as exc:  # noqa: BLE001
            overview.setdefault("warnings", []).append(f"concept_rankings_failed: {exc}")
    else:
        overview.setdefault("warnings", []).append("concept_rankings_skipped")
    return overview


def _build_fallback_candidates(
    manager: Any,
    market_overview: dict[str, Any],
    strategies: list[dict[str, Any]],
    history_days: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    symbol_to_strategy: dict[str, str] = {}
    for strategy in strategies:
        strategy_id = _text(strategy.get("id"))
        symbol = _text(strategy.get("symbol"))
        for key in _symbol_lookup_keys(symbol):
            symbol_to_strategy[key] = strategy_id

    quote_symbols: dict[str, list[str]] = {}
    for item in market_overview.get("hot_stocks") or []:
        if not isinstance(item, dict):
            continue
        symbol = _canonical_symbol(_text(item.get("code") or item.get("symbol")))
        if not symbol:
            continue
        quote_symbols.setdefault(symbol, []).append("hot_stock")
    for item in market_overview.get("limit_up_pool") or []:
        if not isinstance(item, dict):
            continue
        symbol = _canonical_symbol(_text(item.get("code") or item.get("symbol")))
        if not symbol:
            continue
        quote_symbols.setdefault(symbol, []).append("limit_up_pool")

    for strategy in strategies:
        symbol = _canonical_symbol(_text(strategy.get("symbol")))
        if symbol:
            quote_symbols.setdefault(symbol, []).append("configured_symbol")

    for symbol, tags in quote_symbols.items():
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        try:
            quote = manager.get_realtime_quote(symbol)
            history = manager.get_daily_data(symbol, days=history_days)
        except Exception:
            continue
        history_frame = history[0] if isinstance(history, tuple) and history else history
        closes: list[float] = []
        if hasattr(history_frame, "iterrows"):
            for _, row in history_frame.iterrows():
                close_value = _float(row.get("close"))
                if close_value is not None:
                    closes.append(close_value)
        strategy_id = next((strategy for key, strategy in symbol_to_strategy.items() if key in _symbol_lookup_keys(symbol)), None)
        candidates.append(
            _normalize_market_candidate(
                quote=quote,
                closes=closes,
                strategy_id=strategy_id or "dsa-market",
                source_tags=sorted(set(tags)),
            )
        )
    return sorted(candidates, key=lambda item: float(item.get("score") or 0.0), reverse=True)


def _run_alphasift_screen(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not payload.get("use_alphasift"):
        return None
    from src.config import get_config
    from src.services.alphasift_service import AlphaSiftService

    service = AlphaSiftService(config=get_config())
    try:
        result = service.screen(
            strategy=_text(payload.get("screening_strategy")) or "dual_low",
            market=_text(payload.get("market")) or "cn",
            max_results=_int(payload.get("screening_max_results"), 20),
        )
    except Exception:
        return None
    return _plain(result) if isinstance(result, dict) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()

    payload = _load_payload(args.payload)
    dsa_root = Path(_text(payload.get("dsa_root"))).expanduser().resolve()
    use_alphasift = bool(payload.get("use_alphasift"))
    history_days = _int(payload.get("history_days"), 40)

    if not dsa_root.exists():
        print(json.dumps({"ok": False, "errors": [f"DSA root not found: {dsa_root}"]}, ensure_ascii=True))
        return

    try:
        _prepare_environment(dsa_root, use_alphasift)
        from data_provider import DataFetcherManager
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "errors": [f"failed to load DSA runtime: {exc}"]}, ensure_ascii=True))
        return

    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        strategies = []

    manager = DataFetcherManager()
    strategy_snapshots: dict[str, Any] = {}
    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        strategy_id = _text(strategy.get("id"))
        symbol = _text(strategy.get("symbol"))
        if not strategy_id or not symbol:
            continue
        try:
            strategy_snapshots[strategy_id] = _build_strategy_snapshot(manager, strategy, history_days=history_days)
        except Exception as exc:  # noqa: BLE001
            strategy_snapshots[strategy_id] = {
                "symbol": _canonical_symbol(symbol),
                "error": str(exc),
                "data_source": "dsa",
            }

    market_overview = _manager_market_overview(manager, payload)
    symbol_to_strategy: dict[str, str] = {}
    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        strategy_id = _text(strategy.get("id"))
        symbol = _text(strategy.get("symbol"))
        if not strategy_id or not symbol:
            continue
        for key in _symbol_lookup_keys(symbol):
            symbol_to_strategy[key] = strategy_id

    alphasift_screen = _run_alphasift_screen(payload)
    if alphasift_screen and isinstance(alphasift_screen.get("candidates"), list):
        watchlist_candidates = [
            _normalize_alphasift_candidate(
                item,
                strategy_id=f"dsa-screen:{_text(alphasift_screen.get('strategy')) or 'screening'}",
                symbol_to_strategy=symbol_to_strategy,
            )
            for item in alphasift_screen["candidates"]
            if isinstance(item, dict)
        ]
    else:
        watchlist_candidates = _build_fallback_candidates(manager, market_overview, strategies, history_days)

    output = {
        "ok": True,
        "provider": "dsa",
        "strategy_snapshots": strategy_snapshots,
        "market_overview": market_overview,
        "watchlist_candidates": watchlist_candidates,
        "alphasift_screen": alphasift_screen,
    }
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
