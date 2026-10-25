from __future__ import annotations

import logging
import json
from copy import deepcopy
from datetime import date, datetime, UTC, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..audit.logger import append_jsonl, utc_now_iso, write_json
from ..analysis.review import build_candidate_review_summary, build_candidate_reviews
from ..analysis.watchlist import build_market_observation_summary, build_watchlist_candidate
from ..brokers.base import BrokerAdapter, OrderIntent
from ..data.ifind_client import (
    IfindClient,
    extract_close_series,
    extract_latest_price,
    extract_observed_at,
    extract_pre_close,
)
from ..risk.rules import evaluate_risk
from ..state_store import StateStore
from ..strategies.base import MarketSnapshot
from ..strategies.auction_wave import AuctionWaveStrategy
from ..strategies.breakout import BreakoutStrategy
from .planner import build_order_intent


RECORD_SCHEMA_VERSION = "trader.runtime_record.v1"


def _new_run_id(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return f"{current.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"


def _snapshot_file(runtime_dir: Path, run_day: date, strategy_id: str, symbol: str) -> Path:
    safe_strategy = strategy_id.replace("/", "_").replace(":", "_").replace(" ", "_")
    safe_symbol = symbol.replace("/", "_").replace(":", "_").replace(" ", "_")
    return runtime_dir / "snapshots" / run_day.isoformat() / f"{safe_strategy}__{safe_symbol}.jsonl"


def _read_json_or_default(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _latest_previous_candidates(runtime_dir: Path) -> list[dict[str, Any]]:
    runs_dir = runtime_dir / "runs"
    if not runs_dir.exists() or not runs_dir.is_dir():
        return []
    run_dirs = sorted(
        [item for item in runs_dir.iterdir() if item.is_dir()],
        key=lambda item: item.name,
        reverse=True,
    )
    for run_dir in run_dirs:
        payload = _read_json_or_default(run_dir / "watchlist_candidates.json", [])
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    return []


def _build_strategy(strategy_config: dict[str, Any]):
    strategy_type = str(strategy_config.get("strategy", "breakout")).strip().lower()
    if strategy_type == "auction_wave":
        return AuctionWaveStrategy(strategy_config)
    return BreakoutStrategy(strategy_config)


def _stage_from_action(action: str) -> str:
    if action == "buy":
        return "entry_trigger"
    if action == "sell":
        return "exit_trigger"
    return "observation"


def _base_record(
    *,
    run_id: str,
    strategy_config: dict[str, Any],
    symbol: str,
    record_type: str,
    stage: str,
) -> dict[str, Any]:
    return {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_type": record_type,
        "run_id": run_id,
        "strategy_id": strategy_config["id"],
        "strategy": strategy_config.get("strategy", "breakout"),
        "symbol": symbol,
        "stage": stage,
        "ts": utc_now_iso(),
    }


def _legacy_signal_aliases(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "strategyId": record["strategy_id"],
        "latestPrice": record["latest_price"],
        "riskOk": record["risk_ok"],
        "riskReason": record["risk_reason"],
    }


def _legacy_order_aliases(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **record,
        "strategyId": record["strategy_id"],
        "latestPrice": record["latest_price"],
    }


def _order_plan_record(
    *,
    run_id: str,
    strategy_config: dict[str, Any],
    intent: OrderIntent,
    risk_ok: bool,
    risk_reason: str,
    decision_reason: str,
) -> dict[str, Any]:
    return {
        **_base_record(
            run_id=run_id,
            strategy_config=strategy_config,
            symbol=intent.symbol,
            record_type="order_plan",
            stage="order_plan",
        ),
        "side": intent.side,
        "quantity": intent.quantity,
        "latest_price": intent.latest_price,
        "dry_run": intent.dry_run,
        "risk_ok": risk_ok,
        "risk_reason": risk_reason,
        "decision_reason": decision_reason,
        "metadata": intent.metadata,
    }


def _order_fingerprint(intent: OrderIntent) -> str:
    decision = intent.metadata.get("decision")
    decision_key = ""
    if isinstance(decision, dict):
        decision_key = f"{decision.get('action_type', '')}:{decision.get('rule_id', '')}"
    return "|".join(
        [
            intent.strategy_id,
            intent.symbol,
            intent.side,
            str(intent.quantity),
            decision_key,
        ]
    )


def _is_duplicate_order(previous_entry: dict[str, Any], fingerprint: str) -> bool:
    previous_fingerprint = previous_entry.get("last_order_fingerprint") or previous_entry.get("lastOrderFingerprint")
    return bool(fingerprint and previous_fingerprint == fingerprint)


def _daily_ledger(current_state: dict[str, Any], trading_day: str) -> dict[str, Any]:
    ledgers = current_state.setdefault("order_ledgers", {})
    if not isinstance(ledgers, dict):
        ledgers = {}
        current_state["order_ledgers"] = ledgers
    ledger = ledgers.setdefault(trading_day, {"orders": {}})
    if not isinstance(ledger, dict):
        ledger = {"orders": {}}
        ledgers[trading_day] = ledger
    orders = ledger.setdefault("orders", {})
    if not isinstance(orders, dict):
        ledger["orders"] = {}
    return ledger


def _ledger_order_key(intent: OrderIntent) -> str:
    return "|".join([intent.strategy_id, intent.symbol, intent.side])


def _ledger_order_entry(ledger: dict[str, Any], intent: OrderIntent) -> dict[str, Any] | None:
    orders = ledger.get("orders")
    if not isinstance(orders, dict):
        return None
    entry = orders.get(_ledger_order_key(intent))
    if not isinstance(entry, dict):
        return None
    return entry


def _is_unlocked_ledger_order(ledger: dict[str, Any], intent: OrderIntent, fingerprint: str) -> bool:
    entry = _ledger_order_entry(ledger, intent)
    if not entry:
        return False
    return bool(fingerprint and entry.get("order_fingerprint") == fingerprint and entry.get("guard_active", True) is False)


def _is_duplicate_ledger_order(ledger: dict[str, Any], intent: OrderIntent, fingerprint: str) -> bool:
    entry = _ledger_order_entry(ledger, intent)
    if not entry:
        return False
    if entry.get("guard_active", True) is False:
        return False
    return bool(fingerprint and entry.get("order_fingerprint") == fingerprint)


def _pick_text(payload: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _order_lifecycle_status(order_result: dict[str, Any]) -> str:
    if order_result.get("mode") == "dry_run":
        dry_run = order_result.get("dryRun")
        if isinstance(dry_run, dict) and dry_run.get("accepted") is False:
            return "rejected"
        return "planned"

    order = order_result.get("order")
    if not isinstance(order, dict):
        return "submitted"

    raw_status = (_pick_text(order, ["status", "orderStatus"]) or "").strip().lower()
    if raw_status in {"filled", "done", "executed", "complete", "completed"}:
        return "filled"
    if raw_status in {"partial", "partially_filled", "partial_filled"}:
        return "partial"
    if raw_status in {"rejected", "failed", "error"} or order.get("accepted") is False:
        return "rejected"
    if raw_status in {"canceled", "cancelled"}:
        return "canceled"
    return "submitted"


def _ledger_lifecycle_fields(order_result: dict[str, Any]) -> dict[str, Any]:
    lifecycle_status = _order_lifecycle_status(order_result)
    dry_run = order_result.get("dryRun")
    order = order_result.get("order")
    dry_run_payload = dry_run if isinstance(dry_run, dict) else {}
    order_payload = order if isinstance(order, dict) else {}
    broker_order_id = _pick_text(order_payload, ["orderId", "order_id", "id"])
    broker_order_status = _pick_text(order_payload, ["status", "orderStatus"])

    return {
        "lifecycle_status": lifecycle_status,
        "broker_order_status": broker_order_status,
        "broker_order_id": broker_order_id,
        "broker_provider": _pick_text(order_payload, ["provider"]) or _pick_text(dry_run_payload, ["provider"]),
        "accepted": order_payload.get("accepted", dry_run_payload.get("accepted")),
        "submitted_at": _pick_text(order_payload, ["submittedAt", "queuedAt", "filledAt"]) if lifecycle_status != "planned" else None,
        "filled_at": _pick_text(order_payload, ["filledAt"]) if lifecycle_status == "filled" else None,
        "raw_result": order_result,
    }


def _record_ledger_order(
    *,
    ledger: dict[str, Any],
    intent: OrderIntent,
    fingerprint: str,
    run_id: str,
    risk_reason: str,
    decision_reason: str,
    order_result: dict[str, Any],
) -> None:
    orders = ledger.setdefault("orders", {})
    if not isinstance(orders, dict):
        orders = {}
        ledger["orders"] = orders
    existing = orders.get(_ledger_order_key(intent))
    guard_history = existing.get("guard_history") if isinstance(existing, dict) else []
    if not isinstance(guard_history, list):
        guard_history = []
    orders[_ledger_order_key(intent)] = {
        "order_fingerprint": fingerprint,
        "strategy_id": intent.strategy_id,
        "symbol": intent.symbol,
        "side": intent.side,
        "quantity": intent.quantity,
        "latest_price": intent.latest_price,
        "dry_run": intent.dry_run,
        "guard_active": True,
        "retry_allowed": False,
        "guard_history": guard_history,
        "run_id": run_id,
        "risk_reason": risk_reason,
        "decision_reason": decision_reason,
        "recorded_at": utc_now_iso(),
        **_ledger_lifecycle_fields(order_result),
    }


def _suppressed_order_record(
    *,
    run_id: str,
    strategy_config: dict[str, Any],
    intent: OrderIntent,
    risk_ok: bool,
    risk_reason: str,
    decision_reason: str,
    fingerprint: str,
    guard_reason: str,
    trading_day: str,
) -> dict[str, Any]:
    return {
        **_base_record(
            run_id=run_id,
            strategy_config=strategy_config,
            symbol=intent.symbol,
            record_type="suppressed_order",
            stage="order_guard",
        ),
        "side": intent.side,
        "quantity": intent.quantity,
        "latest_price": intent.latest_price,
        "dry_run": intent.dry_run,
        "risk_ok": risk_ok,
        "risk_reason": risk_reason,
        "decision_reason": decision_reason,
        "order_fingerprint": fingerprint,
        "guard_reason": guard_reason,
        "trading_day": trading_day,
    }


def _error_record(
    *,
    run_id: str,
    strategy_config: dict[str, Any],
    symbol: str,
    exc: Exception,
) -> dict[str, Any]:
    return {
        **_base_record(
            run_id=run_id,
            strategy_config=strategy_config,
            symbol=symbol,
            record_type="error",
            stage="strategy_error",
        ),
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }


def _provider_snapshot_payload(
    provider_bundle: dict[str, Any] | None,
    strategy_id: str,
) -> dict[str, Any] | None:
    if not provider_bundle:
        return None
    snapshots = provider_bundle.get("strategy_snapshots")
    if not isinstance(snapshots, dict):
        return None
    snapshot = snapshots.get(strategy_id)
    return snapshot if isinstance(snapshot, dict) else None


def _provider_watchlist_candidates(provider_bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not provider_bundle:
        return []
    payload = provider_bundle.get("watchlist_candidates")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _merge_watchlist_candidates(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for collection in (primary, secondary):
        for item in collection:
            strategy_id = str(item.get("strategy_id") or "")
            symbol = str(item.get("symbol") or "")
            key = (strategy_id, symbol)
            if not symbol or key in seen:
                continue
            seen.add(key)
            merged.append({**item, "run_id": run_id})
    return merged


def _enrich_market_observation_summary(
    summary: dict[str, Any],
    provider_bundle: dict[str, Any] | None,
) -> dict[str, Any]:
    if not provider_bundle:
        return summary
    market_overview = provider_bundle.get("market_overview")
    alphasift_screen = provider_bundle.get("alphasift_screen")
    if isinstance(market_overview, dict):
        summary["provider"] = market_overview.get("provider", "dsa")
        summary["market"] = market_overview.get("market")
        for key in ("market_stats", "hot_stocks", "limit_up_pool", "sector_rankings", "concept_rankings", "warnings"):
            if key in market_overview:
                summary[f"provider_{key}" if key == "warnings" else key] = market_overview.get(key)
    if isinstance(alphasift_screen, dict):
        summary["alphasift_screening"] = {
            "strategy": alphasift_screen.get("strategy"),
            "candidate_count": alphasift_screen.get("candidate_count"),
            "after_filter_count": alphasift_screen.get("after_filter_count"),
            "llm_ranked": alphasift_screen.get("llm_ranked"),
            "warnings": alphasift_screen.get("warnings"),
            "source_errors": alphasift_screen.get("source_errors"),
        }
    return summary


def run_cycle(
    *,
    strategies: list[dict[str, Any]],
    ifind_client: IfindClient,
    broker: BrokerAdapter,
    runtime_dir,
    state_store: StateStore,
    logger: logging.Logger,
) -> dict[str, Any]:
    run_id = _new_run_id()
    run_started_at = utc_now_iso()
    run_dir = runtime_dir / "runs" / run_id
    previous_watchlist_candidates = _latest_previous_candidates(runtime_dir)
    signals_path = runtime_dir / "signals.jsonl"
    orders_path = runtime_dir / "orders.jsonl"
    today = date.today()
    startdate = str(today - timedelta(days=40))
    enddate = str(today)
    current_state = state_store.load()
    current_state.setdefault("strategies", {})
    trading_day = today.isoformat()
    order_ledger = _daily_ledger(current_state, trading_day)
    results: list[dict[str, Any]] = []
    market_records: list[dict[str, Any]] = []
    signal_records: list[dict[str, Any]] = []
    order_records: list[dict[str, Any]] = []
    order_plan_records: list[dict[str, Any]] = []
    suppressed_order_records: list[dict[str, Any]] = []
    error_records: list[dict[str, Any]] = []
    internal_watchlist_candidate_records: list[dict[str, Any]] = []
    provider_bundle = None
    if hasattr(ifind_client, "build_cycle_bundle"):
        provider_bundle = ifind_client.build_cycle_bundle(strategies, today)

    for strategy_config in strategies:
        if not strategy_config.get("enabled", True):
            continue
        symbol = str(strategy_config.get("symbol") or "")
        try:
            logger.info("evaluating strategy=%s symbol=%s", strategy_config["id"], symbol)
            provider_snapshot = _provider_snapshot_payload(provider_bundle, strategy_config["id"])
            if provider_snapshot and "error" not in provider_snapshot:
                realtime_payload = provider_snapshot.get("quote_payload") or {
                    "table": {
                        "latest": [provider_snapshot.get("latest_price")],
                        "preClose": [provider_snapshot.get("pre_close")],
                    },
                    "time": [provider_snapshot.get("observed_at")],
                }
                history_payload = provider_snapshot.get("history_payload") or {
                    "table": {
                        "close": provider_snapshot.get("close_series") or [],
                    }
                }
            else:
                realtime_payload = ifind_client.query_realtime_quotes(symbol)
                history_payload = ifind_client.query_history_quotes(symbol, startdate=startdate, enddate=enddate)
            snapshot_path = _snapshot_file(runtime_dir, today, strategy_config["id"], symbol)
            append_jsonl(
                snapshot_path,
                {
                    "schema_version": RECORD_SCHEMA_VERSION,
                    "record_type": "market_snapshot",
                    "run_id": run_id,
                    "strategy_id": strategy_config["id"],
                    "strategyId": strategy_config["id"],
                    "strategy": strategy_config.get("strategy", "breakout"),
                    "symbol": symbol,
                    "stage": "market_observation",
                    "request_window": {
                        "startdate": startdate,
                        "enddate": enddate,
                    },
                    "requestWindow": {
                        "startdate": startdate,
                        "enddate": enddate,
                    },
                    "realtime_payload": realtime_payload,
                    "history_payload": history_payload,
                    "realtimePayload": realtime_payload,
                    "historyPayload": history_payload,
                },
            )
            if provider_snapshot and "error" not in provider_snapshot:
                snapshot = MarketSnapshot(
                    symbol=str(provider_snapshot.get("symbol") or symbol),
                    latest_price=provider_snapshot.get("latest_price"),
                    pre_close=provider_snapshot.get("pre_close"),
                    close_series=list(provider_snapshot.get("close_series") or []),
                    observed_at=extract_observed_at(realtime_payload),
                )
                data_source = provider_snapshot.get("data_source", "dsa")
            else:
                snapshot = MarketSnapshot(
                    symbol=symbol,
                    latest_price=extract_latest_price(realtime_payload),
                    pre_close=extract_pre_close(realtime_payload),
                    close_series=extract_close_series(history_payload),
                    observed_at=extract_observed_at(realtime_payload),
                )
                data_source = "ifind"
            market_record = {
                **_base_record(
                    run_id=run_id,
                    strategy_config=strategy_config,
                    symbol=symbol,
                    record_type="market_observation",
                    stage="market_observation",
                ),
                "data_source": data_source,
                "request_window": {
                    "startdate": startdate,
                    "enddate": enddate,
                },
                "latest_price": snapshot.latest_price,
                "pre_close": snapshot.pre_close,
                "observed_at": snapshot.observed_at.isoformat(timespec="seconds") if snapshot.observed_at else None,
                "close_series_count": len(snapshot.close_series),
                "snapshot": {
                    "symbol": snapshot.symbol,
                    "latest_price": snapshot.latest_price,
                    "pre_close": snapshot.pre_close,
                    "close_series": snapshot.close_series,
                    "observed_at": snapshot.observed_at.isoformat(timespec="seconds") if snapshot.observed_at else None,
                },
                "snapshot_path": str(snapshot_path),
            }
            market_records.append(market_record)
            strategy = _build_strategy(strategy_config)
            previous_entry = current_state["strategies"].get(strategy_config["id"], {})
            previous_state = previous_entry.get("strategyState", previous_entry)
            market_record["strategy_config"] = deepcopy(strategy_config)
            market_record["prior_state"] = deepcopy(previous_state)
            signal = strategy.evaluate(snapshot, previous_state)
            risk_ok, risk_reason = evaluate_risk(strategy_config, signal.action, signal.target_quantity)
            signal_record = {
                **_base_record(
                    run_id=run_id,
                    strategy_config=strategy_config,
                    symbol=symbol,
                    record_type="signal",
                    stage=_stage_from_action(signal.action),
                ),
                "action": signal.action,
                "decision_reason": signal.reason,
                "reason": signal.reason,
                "target_quantity": signal.target_quantity,
                "latest_price": signal.latest_price,
                "risk_ok": risk_ok,
                "risk_reason": risk_reason,
                "metadata": signal.metadata,
            }
            signal_records.append(signal_record)
            append_jsonl(signals_path, _legacy_signal_aliases(signal_record))
            internal_watchlist_candidate_records.append(
                build_watchlist_candidate(
                    run_id=run_id,
                    strategy_config=strategy_config,
                    snapshot=snapshot,
                    signal=signal,
                    risk_ok=risk_ok,
                    risk_reason=risk_reason,
                )
            )

            order_result = None
            order_fingerprint = previous_entry.get("last_order_fingerprint") or previous_entry.get("lastOrderFingerprint")
            order_guard_reason = None
            intent = build_order_intent(strategy_config, signal)
            if intent:
                fingerprint = _order_fingerprint(intent)
                ledger_unlocked_for_retry = _is_unlocked_ledger_order(order_ledger, intent, fingerprint)
                if _is_duplicate_ledger_order(order_ledger, intent, fingerprint):
                    order_guard_reason = "duplicate_daily_order_ledger"
                elif not ledger_unlocked_for_retry and _is_duplicate_order(previous_entry, fingerprint):
                    order_guard_reason = "duplicate_order_fingerprint"

                if order_guard_reason:
                    order_result = {
                        "mode": "suppressed",
                        "reason": order_guard_reason,
                        "order_fingerprint": fingerprint,
                        "trading_day": trading_day,
                    }
                    suppressed_order_records.append(
                        _suppressed_order_record(
                            run_id=run_id,
                            strategy_config=strategy_config,
                            intent=intent,
                            risk_ok=risk_ok,
                            risk_reason=risk_reason,
                            decision_reason=signal.reason,
                            fingerprint=fingerprint,
                            guard_reason=order_guard_reason,
                            trading_day=trading_day,
                        )
                    )
                else:
                    order_plan_records.append(
                        _order_plan_record(
                            run_id=run_id,
                            strategy_config=strategy_config,
                            intent=intent,
                            risk_ok=risk_ok,
                            risk_reason=risk_reason,
                            decision_reason=signal.reason,
                        )
                    )
                    if risk_ok:
                        dry_run_result = broker.dry_run_order(intent)
                        if intent.dry_run:
                            order_result = {
                                "mode": "dry_run",
                                "dryRun": dry_run_result,
                            }
                        else:
                            order_result = {
                                "mode": "execute",
                                "dryRun": dry_run_result,
                                "order": broker.place_order(intent),
                            }
                        order_fingerprint = fingerprint
                        order_record = {
                            **_base_record(
                                run_id=run_id,
                                strategy_config=strategy_config,
                                symbol=intent.symbol,
                                record_type="order",
                                stage="order_execution",
                            ),
                            "side": intent.side,
                            "quantity": intent.quantity,
                            "latest_price": intent.latest_price,
                            "decision_reason": signal.reason,
                            "risk_ok": risk_ok,
                            "risk_reason": risk_reason,
                            "result": order_result,
                        }
                        order_records.append(order_record)
                        append_jsonl(orders_path, _legacy_order_aliases(order_record))
                        _record_ledger_order(
                            ledger=order_ledger,
                            intent=intent,
                            fingerprint=fingerprint,
                            run_id=run_id,
                            risk_reason=risk_reason,
                            decision_reason=signal.reason,
                            order_result=order_result,
                        )

            next_state = signal.metadata.get("next_state", previous_state)
            current_state["strategies"][strategy_config["id"]] = {
                "run_id": run_id,
                "runId": run_id,
                "symbol": symbol,
                "strategy": strategy_config.get("strategy", "breakout"),
                "last_action": signal.action,
                "lastAction": signal.action,
                "last_reason": signal.reason,
                "lastReason": signal.reason,
                "decision_reason": signal.reason,
                "decisionReason": signal.reason,
                "latest_price": signal.latest_price,
                "latestPrice": signal.latest_price,
                "pre_close": snapshot.pre_close,
                "preClose": snapshot.pre_close,
                "observed_at": snapshot.observed_at.isoformat(timespec="seconds") if snapshot.observed_at else None,
                "observedAt": snapshot.observed_at.isoformat(timespec="seconds") if snapshot.observed_at else None,
                "risk_ok": risk_ok,
                "riskOk": risk_ok,
                "risk_reason": risk_reason,
                "riskReason": risk_reason,
                "snapshot_path": str(snapshot_path),
                "snapshotPath": str(snapshot_path),
                "strategy_state": next_state,
                "strategyState": next_state,
                "metadata": signal.metadata,
                "order_result": order_result,
                "orderResult": order_result,
                "last_order_fingerprint": order_fingerprint,
                "lastOrderFingerprint": order_fingerprint,
                "last_order_guard_reason": order_guard_reason,
                "lastOrderGuardReason": order_guard_reason,
                "last_order_ledger_day": trading_day,
                "lastOrderLedgerDay": trading_day,
            }
            results.append(current_state["strategies"][strategy_config["id"]])
        except Exception as exc:
            logger.exception("strategy failed strategy=%s symbol=%s", strategy_config.get("id"), symbol)
            error_record = _error_record(
                run_id=run_id,
                strategy_config=strategy_config,
                symbol=symbol,
                exc=exc,
            )
            error_records.append(error_record)
            current_state["strategies"][strategy_config["id"]] = {
                "run_id": run_id,
                "runId": run_id,
                "symbol": symbol,
                "strategy": strategy_config.get("strategy", "breakout"),
                "last_action": "error",
                "lastAction": "error",
                "last_reason": str(exc),
                "lastReason": str(exc),
                "decision_reason": str(exc),
                "decisionReason": str(exc),
                "risk_ok": False,
                "riskOk": False,
                "risk_reason": "strategy evaluation failed",
                "riskReason": "strategy evaluation failed",
                "metadata": {
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
                "order_result": None,
                "orderResult": None,
            }
            results.append(current_state["strategies"][strategy_config["id"]])

    state_store.save(current_state)
    watchlist_candidate_records = _merge_watchlist_candidates(
        _provider_watchlist_candidates(provider_bundle),
        internal_watchlist_candidate_records,
        run_id=run_id,
    )
    market_observation_summary = build_market_observation_summary(watchlist_candidate_records, len(error_records))
    market_observation_summary = _enrich_market_observation_summary(market_observation_summary, provider_bundle)
    candidate_reviews = build_candidate_reviews(
        run_id=run_id,
        previous_candidates=previous_watchlist_candidates,
        current_candidates=watchlist_candidate_records,
    )
    candidate_review_summary = build_candidate_review_summary(candidate_reviews)
    summary = {
        "run_id": run_id,
        "run_started_at": run_started_at,
        "run_finished_at": utc_now_iso(),
        "brokerStatus": broker.broker_status(),
        "broker_status": broker.broker_status(),
        "strategyCount": len(results),
        "strategy_count": len(results),
        "signal_count": len(signal_records),
        "order_plan_count": len(order_plan_records),
        "order_count": len(order_records),
        "suppressed_order_count": len(suppressed_order_records),
        "error_count": len(error_records),
        "watchlist_candidate_count": len(watchlist_candidate_records),
        "candidate_review_count": len(candidate_reviews),
        "market_observation": market_observation_summary,
        "candidate_review": candidate_review_summary,
        "results": results,
    }
    write_json(run_dir / "market.json", market_records)
    write_json(run_dir / "market_observation.json", market_observation_summary)
    write_json(run_dir / "watchlist_candidates.json", watchlist_candidate_records)
    write_json(run_dir / "candidate_reviews.json", candidate_reviews)
    write_json(run_dir / "candidate_review.json", candidate_review_summary)
    write_json(run_dir / "signals.json", signal_records)
    write_json(run_dir / "orders.json", order_records)
    write_json(run_dir / "order_plans.json", order_plan_records)
    write_json(run_dir / "suppressed_orders.json", suppressed_order_records)
    write_json(run_dir / "errors.json", error_records)
    write_json(run_dir / "summary.json", summary)
    return summary
