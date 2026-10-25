from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from hub.models import (
    AgentWorkspaceActionResponse,
    AgentWorkspaceConfigUpdateResponse,
    AgentWorkspaceSummaryResponse,
)


class TraderAdapter:
    SUPPORTED_BROKERS = {"paper", "eastmoney_stub"}

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def exists(self) -> bool:
        return self.root_dir.exists()

    @property
    def env_file(self) -> Path:
        return self.root_dir / "config" / "trader.env"

    @property
    def env_template(self) -> Path:
        return self.root_dir / "config" / "trader.env.template"

    @property
    def strategy_template(self) -> Path:
        return self.root_dir / "config" / "strategies.example.yaml"

    def read_env_values(self) -> dict[str, str]:
        source = self.env_file if self.env_file.exists() else self.env_template
        values: dict[str, str] = {}
        if not source.exists():
            return values
        for line in source.read_text(encoding="utf-8").splitlines():
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#") or "=" not in trimmed:
                continue
            key, value = trimmed.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def write_env_values(self, values: dict[str, str]) -> None:
        env_order = [
            "TRADER_BOT_NAME",
            "TRADER_BIND_MODE",
            "TRADER_LOOP_INTERVAL_SECONDS",
            "TRADER_LOG_LEVEL",
            "TRADER_RUNTIME_DIR",
            "TRADER_STRATEGY_FILE",
            "TRADER_BROKER",
            "IFIND_BASE_URL",
            "IFIND_ACCESS_TOKEN",
            "IFIND_REFRESH_TOKEN",
            "IFIND_REQUEST_TIMEOUT_MS",
            "TRADER_EASTMONEY_ACCOUNT_ID",
        ]
        lines: list[str] = []
        for key in env_order:
            if key in values:
                lines.append(f"{key}={values[key]}")
        for key, value in values.items():
            if key in env_order:
                continue
            lines.append(f"{key}={value}")
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def runtime_dir(self, env_values: dict[str, str]) -> Path:
        runtime_dir = env_values.get("TRADER_RUNTIME_DIR", "runtime")
        runtime_path = Path(runtime_dir)
        return runtime_path if runtime_path.is_absolute() else self.root_dir / runtime_path

    def strategy_file(self, env_values: dict[str, str]) -> Path:
        strategy_file = env_values.get("TRADER_STRATEGY_FILE", "config/strategies.yaml")
        strategy_path = Path(strategy_file)
        return strategy_path if strategy_path.is_absolute() else self.root_dir / strategy_path

    @staticmethod
    def read_json_file_or_default(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in [line for line in lines if line.strip()][-limit:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def read_recent_run_batches(runtime_dir: Path, limit: int) -> list[dict[str, Any]]:
        runs_dir = runtime_dir / "runs"
        if not runs_dir.exists() or not runs_dir.is_dir():
            return []

        batches: list[dict[str, Any]] = []
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            summary_path = run_dir / "summary.json"
            if not summary_path.exists():
                continue
            try:
                loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(loaded, dict):
                continue
            run_id = str(loaded.get("run_id") or run_dir.name)
            batches.append(
                {
                    "run_id": run_id,
                    "run_started_at": loaded.get("run_started_at"),
                    "run_finished_at": loaded.get("run_finished_at"),
                    "strategy_count": loaded.get("strategy_count", loaded.get("strategyCount", 0)),
                    "signal_count": loaded.get("signal_count", 0),
                    "order_plan_count": loaded.get("order_plan_count", 0),
                    "order_count": loaded.get("order_count", 0),
                    "suppressed_order_count": loaded.get("suppressed_order_count", 0),
                    "error_count": loaded.get("error_count", 0),
                    "summary_path": str(summary_path),
                    "run_dir": str(run_dir),
                }
            )
        return sorted(
            batches,
            key=lambda item: str(item.get("run_finished_at") or item.get("run_started_at") or item.get("run_id") or ""),
            reverse=True,
        )[:limit]

    @classmethod
    def read_latest_run_artifacts(
        cls, recent_runs: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        artifact_run = next(
            (
                item
                for item in recent_runs
                if isinstance(item.get("run_dir"), str)
                and any(
                    (Path(item["run_dir"]) / name).exists()
                    for name in ("market_observation.json", "watchlist_candidates.json")
                )
            ),
            None,
        )
        if artifact_run is None:
            return {}, [], {}, []
        run_dir_text = artifact_run.get("run_dir")
        if not isinstance(run_dir_text, str) or not run_dir_text.strip():
            return {}, [], {}, []
        run_dir = Path(run_dir_text)
        market_observation = cls.read_json_file_or_default(run_dir / "market_observation.json")
        candidate_review = cls.read_json_file_or_default(run_dir / "candidate_review.json")
        candidates_payload = cls.read_json_file_or_default(run_dir / "watchlist_candidates.json")
        reviews_payload = cls.read_json_file_or_default(run_dir / "candidate_reviews.json")
        candidates: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        if isinstance(candidates_payload, list):
            candidates = [item for item in candidates_payload if isinstance(item, dict)]
        else:
            try:
                loaded = json.loads((run_dir / "watchlist_candidates.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = []
            if isinstance(loaded, list):
                candidates = [item for item in loaded if isinstance(item, dict)]
        if isinstance(reviews_payload, list):
            reviews = [item for item in reviews_payload if isinstance(item, dict)]
        else:
            try:
                loaded = json.loads((run_dir / "candidate_reviews.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = []
            if isinstance(loaded, list):
                reviews = [item for item in loaded if isinstance(item, dict)]
        return market_observation, candidates, candidate_review, reviews

    @staticmethod
    def read_text_tail(path: Path, limit: int) -> str:
        if not path.exists():
            return ""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-limit:])

    @staticmethod
    def pick_text(payload: dict[str, Any], keys: list[str]) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def pick_number(payload: dict[str, Any], keys: list[str]) -> float | int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return value
        return None

    @staticmethod
    def read_trader_strategies(path: Path) -> list[Any]:
        if not path.exists():
            return []
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        if not isinstance(parsed, dict):
            return []
        strategies = parsed.get("strategies", [])
        return strategies if isinstance(strategies, list) else []

    def build_strategy_snapshots(
        self,
        strategies: list[Any],
        state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int, str | None, str | None, str | None, str | None]:
        strategy_state_map = state.get("strategies", {})
        if not isinstance(strategy_state_map, dict):
            strategy_state_map = {}

        snapshots: list[dict[str, Any]] = []
        total_position_quantity = 0
        last_run_at: str | None = None
        last_action: str | None = None
        last_reason: str | None = None
        last_snapshot_path: str | None = None

        for strategy in strategies:
            if not isinstance(strategy, dict):
                continue

            strategy_id = str(strategy.get("id", "")).strip()
            runtime_state = strategy_state_map.get(strategy_id, {})
            if not isinstance(runtime_state, dict):
                runtime_state = {}

            strategy_state = runtime_state.get("strategyState", {})
            if not isinstance(strategy_state, dict):
                strategy_state = {}

            position_quantity = 0
            for candidate in (
                strategy_state.get("remaining_quantity"),
                runtime_state.get("position_quantity"),
                strategy.get("position_quantity"),
            ):
                if isinstance(candidate, int):
                    position_quantity = max(position_quantity, candidate)

            total_position_quantity += position_quantity

            observed_at = self.pick_text(runtime_state, ["observedAt", "observed_at"])
            snapshot = {
                "id": strategy_id,
                "name": str(strategy.get("name") or strategy_id or "-"),
                "symbol": str(strategy.get("symbol") or runtime_state.get("symbol") or "-"),
                "strategy": str(strategy.get("strategy") or runtime_state.get("strategy") or "-"),
                "timeframe": str(strategy.get("timeframe") or "-"),
                "enabled": bool(strategy.get("enabled", True)),
                "lastAction": self.pick_text(runtime_state, ["lastAction", "last_action"]) or "-",
                "lastReason": self.pick_text(runtime_state, ["lastReason", "last_reason"]) or "-",
                "latestPrice": self.pick_number(runtime_state, ["latestPrice", "latest_price"]),
                "observedAt": observed_at,
                "riskOk": runtime_state.get("riskOk"),
                "riskReason": self.pick_text(runtime_state, ["riskReason", "risk_reason"]),
                "positionQuantity": position_quantity,
            }
            snapshots.append(snapshot)

            if observed_at and (last_run_at is None or observed_at > last_run_at):
                last_run_at = observed_at
                last_action = str(snapshot["lastAction"])
                last_reason = str(snapshot["lastReason"])
                snapshot_path = runtime_state.get("snapshotPath")
                last_snapshot_path = str(snapshot_path) if isinstance(snapshot_path, str) and snapshot_path.strip() else None

        return snapshots, total_position_quantity, last_run_at, last_action, last_reason, last_snapshot_path

    @staticmethod
    def utc_now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def ledger_order_key(strategy_id: str, symbol: str, side: str) -> str:
        return "|".join([strategy_id, symbol, side])

    @staticmethod
    def read_order_ledger_entries(state: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        ledgers = state.get("order_ledgers")
        if not isinstance(ledgers, dict):
            return []

        entries: list[dict[str, Any]] = []
        for trading_day, ledger in ledgers.items():
            if not isinstance(ledger, dict):
                continue
            orders = ledger.get("orders")
            if not isinstance(orders, dict):
                continue
            for order_key, entry in orders.items():
                if not isinstance(entry, dict):
                    continue
                entries.append(
                    {
                        **entry,
                        "trading_day": str(trading_day),
                        "order_key": str(order_key),
                        "guard_active": bool(entry.get("guard_active", True)),
                        "retry_allowed": bool(entry.get("retry_allowed", False)),
                    }
                )

        return sorted(
            entries,
            key=lambda item: str(
                item.get("recorded_at")
                or item.get("submitted_at")
                or item.get("filled_at")
                or item.get("unlocked_at")
                or item.get("trading_day")
                or ""
            ),
            reverse=True,
        )[:limit]

    @staticmethod
    def path_status(path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "exists": path.exists(),
            "is_dir": path.is_dir(),
            "parent_exists": path.parent.exists(),
        }

    @staticmethod
    def broker_status_from_env(broker: str, env_values: dict[str, str]) -> dict[str, Any]:
        if broker == "paper":
            return {
                "provider": "paper",
                "mode": "paper",
                "liveTradingEnabled": False,
                "configured": True,
            }
        if broker == "eastmoney_stub":
            account_id = env_values.get("TRADER_EASTMONEY_ACCOUNT_ID")
            return {
                "provider": "eastmoney_stub",
                "mode": "stub",
                "liveTradingEnabled": False,
                "configured": bool(account_id),
                "accountIdConfigured": bool(account_id),
            }
        return {
            "provider": broker,
            "configured": False,
            "liveTradingEnabled": False,
        }

    def diagnostics(self) -> dict[str, Any]:
        env_values = self.read_env_values()
        broker = env_values.get("TRADER_BROKER", "paper").strip().lower()
        runtime_dir = self.runtime_dir(env_values)
        strategy_file = self.strategy_file(env_values)
        errors: list[str] = []
        warnings: list[str] = []

        if broker not in self.SUPPORTED_BROKERS:
            errors.append(f"unsupported broker: {broker}")
        if broker == "eastmoney_stub" and not env_values.get("TRADER_EASTMONEY_ACCOUNT_ID"):
            warnings.append("TRADER_EASTMONEY_ACCOUNT_ID is not configured for eastmoney_stub")

        access_token_configured = bool(env_values.get("IFIND_ACCESS_TOKEN"))
        refresh_token_configured = bool(env_values.get("IFIND_REFRESH_TOKEN"))
        if not (access_token_configured or refresh_token_configured):
            errors.append("IFIND_ACCESS_TOKEN or IFIND_REFRESH_TOKEN is required for live data")
        elif access_token_configured and not refresh_token_configured:
            warnings.append("IFIND_REFRESH_TOKEN is not configured; access token refresh will be unavailable")

        strategies: list[Any] = []
        if not strategy_file.exists():
            errors.append(f"strategy file not found: {strategy_file}")
        else:
            strategies = self.read_trader_strategies(strategy_file)

        strategy_errors: list[str] = []
        strategy_ids: list[str] = []
        symbols: list[str] = []
        enabled_count = 0
        seen_ids: set[str] = set()
        for index, item in enumerate(strategies):
            if not isinstance(item, dict):
                strategy_errors.append(f"strategy[{index}] must be an object")
                continue
            strategy_id = str(item.get("id") or "").strip()
            symbol = str(item.get("symbol") or "").strip()
            strategy_type = str(item.get("strategy") or "").strip()
            if bool(item.get("enabled", True)):
                enabled_count += 1
            if strategy_id:
                strategy_ids.append(strategy_id)
                if strategy_id in seen_ids:
                    strategy_errors.append(f"duplicate strategy id: {strategy_id}")
                seen_ids.add(strategy_id)
            else:
                strategy_errors.append(f"strategy[{index}] missing id")
            if symbol:
                symbols.append(symbol)
            else:
                strategy_errors.append(f"strategy[{index}] missing symbol")
            if not strategy_type:
                strategy_errors.append(f"strategy[{index}] missing strategy")

        errors.extend(strategy_errors)
        if strategies and enabled_count == 0:
            warnings.append("no enabled strategy")
        if runtime_dir.exists() and not runtime_dir.is_dir():
            errors.append(f"runtime path is not a directory: {runtime_dir}")
        if not runtime_dir.exists() and not runtime_dir.parent.exists():
            warnings.append(f"runtime parent does not exist yet: {runtime_dir.parent}")

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "config": {
                "env_file": str(self.env_file),
                "env_file_exists": self.env_file.exists(),
                "runtime_dir": str(runtime_dir),
                "strategy_file": str(strategy_file),
                "loop_interval_seconds": int(env_values.get("TRADER_LOOP_INTERVAL_SECONDS", "300") or "300"),
                "log_level": env_values.get("TRADER_LOG_LEVEL", "INFO"),
            },
            "data_source": {
                "provider": "ifind",
                "base_url": env_values.get("IFIND_BASE_URL", "https://quantapi.51ifind.com/api/v1").rstrip("/"),
                "access_token_configured": access_token_configured,
                "refresh_token_configured": refresh_token_configured,
                "timeout_ms": int(env_values.get("IFIND_REQUEST_TIMEOUT_MS", "30000") or "30000"),
            },
            "broker": {
                "provider": broker,
                "supported": broker in self.SUPPORTED_BROKERS,
                "status": self.broker_status_from_env(broker, env_values),
            },
            "runtime": {
                "runtime_dir": self.path_status(runtime_dir),
                "state_file": {
                    "path": str(runtime_dir / "state.json"),
                    "exists": (runtime_dir / "state.json").exists(),
                },
            },
            "strategies": {
                "count": len(strategies),
                "enabled_count": enabled_count,
                "disabled_count": max(0, len(strategies) - enabled_count),
                "ids": strategy_ids,
                "symbols": sorted(set(symbols)),
            },
        }

    def init_config(self) -> AgentWorkspaceActionResponse:
        env_values = self.read_env_values()
        strategy_path = self.strategy_file(env_values)
        files = [
            {
                "kind": "env",
                "source": self.env_template,
                "target": self.env_file,
            },
            {
                "kind": "strategy",
                "source": self.strategy_template,
                "target": strategy_path,
            },
        ]
        results: list[dict[str, str]] = []
        errors: list[str] = []
        for item in files:
            source = item["source"]
            target = item["target"]
            result = {
                "kind": str(item["kind"]),
                "source": str(source),
                "target": str(target),
                "status": "exists",
            }
            if target.exists():
                results.append(result)
                continue
            if not source.exists():
                result["status"] = "missing_template"
                errors.append(f"template not found: {source}")
                results.append(result)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            result["status"] = "created"
            results.append(result)

        payload = {
            "ok": not errors,
            "errors": errors,
            "files": results,
        }
        return AgentWorkspaceActionResponse(
            executed=not errors,
            action="init_config",
            stdout=json.dumps(payload, ensure_ascii=True),
        )

    @staticmethod
    def read_last_cycle_metrics(service_log_path: Path) -> tuple[int, int]:
        if not service_log_path.exists():
            return 0, 0
        try:
            lines = service_log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0, 0

        for line in reversed(lines):
            marker = "cycle result="
            if marker not in line:
                continue
            payload_text = line.split(marker, 1)[1].strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                return 0, 0
            strategy_count = payload.get("strategyCount")
            if not isinstance(strategy_count, int):
                return 0, 0
            # Current trader runtime performs 1 realtime quote request and 1 history request per enabled strategy.
            return strategy_count, strategy_count * 2
        return 0, 0

    @staticmethod
    def trader_pid(runtime_dir: Path) -> int | None:
        pid_file = runtime_dir / "trader.pid"
        if not pid_file.exists():
            return None
        try:
            return int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def is_process_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def summary(self) -> AgentWorkspaceSummaryResponse:
        missing_runtime = self.root_dir / "runtime"
        missing_strategy = self.root_dir / "config" / "strategies.yaml"
        missing_log = missing_runtime / "logs" / "service.log"
        missing_state = missing_runtime / "state.json"
        if not self.exists():
            return AgentWorkspaceSummaryResponse(
                available=False,
                broker="unavailable",
                running=False,
                pid=None,
                runtime_dir=str(missing_runtime),
                strategy_file=str(missing_strategy),
                state_file=str(missing_state),
                service_log_path=str(missing_log),
                strategies=[],
                state={},
                recent_signals=[],
                recent_orders=[],
                recent_runs=[],
                order_ledger_entries=[],
                diagnostics={
                    "ok": False,
                    "errors": [f"trader workspace not found: {self.root_dir}"],
                    "warnings": [],
                },
                market_observation={},
                watchlist_candidates=[],
                candidate_review={},
                candidate_reviews=[],
                service_log_tail="",
                strategy_count=0,
                signal_count=0,
                order_count=0,
                last_signal_at=None,
                last_order_at=None,
                last_run_at=None,
                last_action=None,
                last_reason=None,
                active_position_quantity=0,
                strategy_snapshots=[],
                last_cycle_strategy_count=0,
                last_cycle_request_count=0,
                last_snapshot_path=None,
            )

        env_values = self.read_env_values()
        broker = env_values.get("TRADER_BROKER", "paper")
        runtime_dir = self.runtime_dir(env_values)
        strategy_file = self.strategy_file(env_values)
        pid = self.trader_pid(runtime_dir)
        running = pid is not None and self.is_process_running(pid)

        state_file = runtime_dir / "state.json"
        signals_file = runtime_dir / "signals.jsonl"
        orders_file = runtime_dir / "orders.jsonl"
        service_log_path = runtime_dir / "logs" / "service.log"
        strategies = self.read_trader_strategies(strategy_file)
        state = self.read_json_file_or_default(state_file)
        recent_signals = self.read_jsonl_tail(signals_file, 8)
        recent_orders = self.read_jsonl_tail(orders_file, 8)
        recent_runs = self.read_recent_run_batches(runtime_dir, 8)
        artifact_runs = self.read_recent_run_batches(runtime_dir, 256)
        market_observation, watchlist_candidates, candidate_review, candidate_reviews = self.read_latest_run_artifacts(artifact_runs)
        order_ledger_entries = self.read_order_ledger_entries(state, 24)
        diagnostics = self.diagnostics()
        strategy_snapshots, active_position_quantity, last_run_at, last_action, last_reason, last_snapshot_path = self.build_strategy_snapshots(
            strategies,
            state,
        )
        last_signal_at = self.pick_text(recent_signals[-1], ["ts", "timestamp", "created_at", "time"]) if recent_signals else None
        last_order_at = self.pick_text(recent_orders[-1], ["ts", "timestamp", "created_at", "time"]) if recent_orders else None
        last_cycle_strategy_count, last_cycle_request_count = self.read_last_cycle_metrics(service_log_path)

        return AgentWorkspaceSummaryResponse(
            available=True,
            broker=broker,
            running=running,
            pid=pid if running else None,
            runtime_dir=str(runtime_dir),
            strategy_file=str(strategy_file),
            state_file=str(state_file),
            service_log_path=str(service_log_path),
            strategies=strategies,
            state=state,
            recent_signals=recent_signals,
            recent_orders=recent_orders,
            recent_runs=recent_runs,
            order_ledger_entries=order_ledger_entries,
            diagnostics=diagnostics,
            market_observation=market_observation,
            watchlist_candidates=watchlist_candidates,
            candidate_review=candidate_review,
            candidate_reviews=candidate_reviews,
            service_log_tail=self.read_text_tail(service_log_path, 40),
            strategy_count=len(strategy_snapshots),
            signal_count=len(recent_signals),
            order_count=len(recent_orders),
            last_signal_at=last_signal_at,
            last_order_at=last_order_at,
            last_run_at=last_run_at,
            last_action=last_action,
            last_reason=last_reason,
            active_position_quantity=active_position_quantity,
            strategy_snapshots=strategy_snapshots,
            last_cycle_strategy_count=last_cycle_strategy_count,
            last_cycle_request_count=last_cycle_request_count,
            last_snapshot_path=last_snapshot_path,
        )

    def update_config(
        self,
        broker: str,
        strategy: dict[str, Any],
        strategy_id: str | None = None,
    ) -> AgentWorkspaceConfigUpdateResponse:
        normalized_broker = broker.strip().lower()
        if normalized_broker not in {"paper", "eastmoney_stub"}:
            raise ValueError("trader broker must be paper or eastmoney_stub")

        target_strategy_id = str(strategy_id or "").strip()
        next_strategy_id = str(strategy.get("id", "")).strip()
        symbol = str(strategy.get("symbol", "")).strip()
        if not next_strategy_id or not symbol:
            raise ValueError("strategy id and symbol are required")

        env_values = self.read_env_values()
        env_values["TRADER_BROKER"] = normalized_broker
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.write_env_values(env_values)

        strategy_path = self.strategy_file(env_values)
        strategy_path.parent.mkdir(parents=True, exist_ok=True)
        strategies = self.read_trader_strategies(strategy_path)
        saved = False
        payload_strategies: list[Any] = []
        match_id = target_strategy_id or next_strategy_id
        for item in strategies:
            if not isinstance(item, dict):
                payload_strategies.append(item)
                continue
            current_id = str(item.get("id", "")).strip()
            if current_id == match_id:
                payload_strategies.append(strategy)
                saved = True
            else:
                payload_strategies.append(item)

        if not saved:
            payload_strategies.append(strategy)

        payload = {"strategies": payload_strategies}
        strategy_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        return AgentWorkspaceConfigUpdateResponse(
            saved=True,
            broker=normalized_broker,
            strategyCount=len(payload_strategies),
        )

    def unlock_order_guard(self, payload: dict[str, Any]) -> AgentWorkspaceActionResponse:
        trading_day = str(payload.get("trading_day") or "").strip()
        strategy_id = str(payload.get("strategy_id") or "").strip()
        symbol = str(payload.get("symbol") or "").strip()
        side = str(payload.get("side") or "").strip().lower()
        reason = str(payload.get("reason") or "").strip()
        actor = str(payload.get("actor") or "hub").strip() or "hub"

        if not all([trading_day, strategy_id, symbol, side, reason]):
            raise ValueError("trading_day, strategy_id, symbol, side and reason are required")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        env_values = self.read_env_values()
        runtime_dir = self.runtime_dir(env_values)
        state_path = runtime_dir / "state.json"
        state = self.read_json_file_or_default(state_path)
        ledgers = state.get("order_ledgers")
        if not isinstance(ledgers, dict):
            raise KeyError("order_ledgers not found in state")
        ledger = ledgers.get(trading_day)
        if not isinstance(ledger, dict):
            raise KeyError(f"order ledger not found for trading day: {trading_day}")
        orders = ledger.get("orders")
        if not isinstance(orders, dict):
            raise KeyError(f"orders not found for trading day: {trading_day}")

        order_key = self.ledger_order_key(strategy_id, symbol, side)
        entry = orders.get(order_key)
        if not isinstance(entry, dict):
            raise KeyError(f"ledger order not found: {order_key}")

        unlocked_at = self.utc_now_iso()
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

        runtime_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        result = {
            "unlocked": True,
            "trading_day": trading_day,
            "order_key": order_key,
            "entry": entry,
        }
        return AgentWorkspaceActionResponse(
            executed=True,
            action="unlock_order",
            stdout=json.dumps(result, ensure_ascii=True),
        )

    def run_action(self, action: str) -> AgentWorkspaceActionResponse:
        if action == "diagnose":
            return AgentWorkspaceActionResponse(
                executed=True,
                action=action,
                stdout=json.dumps(self.diagnostics(), ensure_ascii=True),
            )
        if action == "init_config":
            return self.init_config()

        script_name_by_action = {
            "run_once": "run_once.sh",
            "start": "start_bot.sh",
            "stop": "stop_bot.sh",
        }
        script_name = script_name_by_action.get(action)
        if script_name is None:
            raise ValueError(f"unsupported trader action: {action}")

        script_path = (self.root_dir / "scripts" / script_name).resolve()
        if not script_path.exists():
            raise FileNotFoundError(f"missing trader script: {script_path}")

        try:
            completed = subprocess.run(
                [str(script_path)],
                cwd=self.root_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = (exc.stdout or "").strip()
            stderr = (exc.stderr or "").strip()
            if stderr:
                message = f"{message}\n{stderr}".strip() if message else stderr
            raise OSError(message or f"trader action failed: {action}") from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stderr:
            stdout = f"{stdout}\n{stderr}".strip() if stdout else stderr
        return AgentWorkspaceActionResponse(
            executed=True,
            action=action,
            stdout=stdout,
        )
