from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..audit.logger import utc_now_iso, write_json
from ..data.ifind_client import (
    extract_close_series,
    extract_latest_price,
    extract_observed_at,
    extract_pre_close,
)
from ..risk.rules import evaluate_risk
from ..strategies.base import MarketSnapshot
from .planner import build_order_intent
from .runner import RECORD_SCHEMA_VERSION, _base_record, _build_strategy, _stage_from_action


def _new_replay_id(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return f"{current.strftime('%Y%m%dT%H%M%SZ')}-replay-{uuid4().hex[:8]}"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _read_snapshot_payload(record: dict[str, Any], source_run_id: str) -> dict[str, Any] | None:
    snapshot_path = record.get("snapshot_path")
    if not isinstance(snapshot_path, str) or not snapshot_path.strip():
        return None
    path = Path(snapshot_path)
    if not path.exists():
        return None

    strategy_id = record.get("strategy_id")
    symbol = record.get("symbol")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    fallback: dict[str, Any] | None = None
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        fallback = payload
        if (
            payload.get("run_id") == source_run_id
            and payload.get("strategy_id") == strategy_id
            and payload.get("symbol") == symbol
        ):
            return payload
    return fallback


def _snapshot_from_market_record(record: dict[str, Any], source_run_id: str) -> MarketSnapshot:
    saved_snapshot = record.get("snapshot")
    if isinstance(saved_snapshot, dict):
        observed_at = _parse_datetime(saved_snapshot.get("observed_at"))
        close_series = saved_snapshot.get("close_series")
        return MarketSnapshot(
            symbol=str(saved_snapshot.get("symbol") or record.get("symbol") or ""),
            latest_price=saved_snapshot.get("latest_price") if isinstance(saved_snapshot.get("latest_price"), (int, float)) else None,
            pre_close=saved_snapshot.get("pre_close") if isinstance(saved_snapshot.get("pre_close"), (int, float)) else None,
            close_series=close_series if isinstance(close_series, list) else [],
            observed_at=observed_at,
        )

    snapshot_payload = _read_snapshot_payload(record, source_run_id)
    if not snapshot_payload:
        raise FileNotFoundError(f"saved market snapshot not found for run_id={source_run_id} strategy_id={record.get('strategy_id')}")

    realtime_payload = snapshot_payload.get("realtime_payload", snapshot_payload.get("realtimePayload"))
    history_payload = snapshot_payload.get("history_payload", snapshot_payload.get("historyPayload"))
    return MarketSnapshot(
        symbol=str(record.get("symbol") or snapshot_payload.get("symbol") or ""),
        latest_price=extract_latest_price(realtime_payload),
        pre_close=extract_pre_close(realtime_payload),
        close_series=extract_close_series(history_payload),
        observed_at=extract_observed_at(realtime_payload),
    )


def _strategy_config_from_record(
    record: dict[str, Any],
    strategies_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    saved_config = record.get("strategy_config")
    if isinstance(saved_config, dict):
        return saved_config

    strategy_id = str(record.get("strategy_id") or "")
    strategy_config = strategies_by_id.get(strategy_id)
    if strategy_config is None:
        raise KeyError(f"strategy config not found for replay strategy_id={strategy_id}")
    return strategy_config


def replay_run(
    *,
    runtime_dir: Path,
    source_run_id: str,
    strategies: list[dict[str, Any]],
    logger: logging.Logger,
) -> dict[str, Any]:
    source_run_dir = runtime_dir / "runs" / source_run_id
    market_path = source_run_dir / "market.json"
    if not market_path.exists():
        raise FileNotFoundError(f"run market file not found: {market_path}")

    market_records = _read_json(market_path)
    if not isinstance(market_records, list):
        raise ValueError(f"run market file must contain a list: {market_path}")

    replay_id = _new_replay_id()
    replay_started_at = utc_now_iso()
    replay_dir = runtime_dir / "replays" / source_run_id / replay_id
    strategies_by_id = {str(item.get("id")): item for item in strategies if isinstance(item, dict) and item.get("id")}

    signal_records: list[dict[str, Any]] = []
    order_plan_records: list[dict[str, Any]] = []

    for record in market_records:
        if not isinstance(record, dict):
            continue
        strategy_config = _strategy_config_from_record(record, strategies_by_id)
        symbol = str(record.get("symbol") or strategy_config.get("symbol") or "")
        snapshot = _snapshot_from_market_record(record, source_run_id)
        prior_state = record.get("prior_state")
        if not isinstance(prior_state, dict):
            prior_state = {}

        logger.info("replaying source_run=%s strategy=%s symbol=%s", source_run_id, strategy_config["id"], symbol)
        signal = _build_strategy(strategy_config).evaluate(snapshot, prior_state)
        risk_ok, risk_reason = evaluate_risk(strategy_config, signal.action, signal.target_quantity)

        signal_record = {
            **_base_record(
                run_id=replay_id,
                strategy_config=strategy_config,
                symbol=symbol,
                record_type="signal",
                stage=_stage_from_action(signal.action),
            ),
            "source_run_id": source_run_id,
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

        intent = build_order_intent(strategy_config, signal)
        if intent:
            order_plan_records.append(
                {
                    **_base_record(
                        run_id=replay_id,
                        strategy_config=strategy_config,
                        symbol=intent.symbol,
                        record_type="order_plan",
                        stage="order_plan",
                    ),
                    "source_run_id": source_run_id,
                    "side": intent.side,
                    "quantity": intent.quantity,
                    "latest_price": intent.latest_price,
                    "dry_run": True,
                    "risk_ok": risk_ok,
                    "risk_reason": risk_reason,
                    "decision_reason": signal.reason,
                    "metadata": intent.metadata,
                    "replay_note": "replay only; no broker order was sent",
                }
            )

    summary = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "replay_id": replay_id,
        "source_run_id": source_run_id,
        "replay_started_at": replay_started_at,
        "replay_finished_at": utc_now_iso(),
        "strategy_count": len(signal_records),
        "signal_count": len(signal_records),
        "order_plan_count": len(order_plan_records),
        "order_count": 0,
        "mode": "replay",
    }

    write_json(replay_dir / "signals.json", signal_records)
    write_json(replay_dir / "order_plans.json", order_plan_records)
    write_json(replay_dir / "orders.json", [])
    write_json(replay_dir / "summary.json", summary)
    return {
        **summary,
        "replay_dir": str(replay_dir),
    }
