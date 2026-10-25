from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig, load_strategy_config


SUPPORTED_BROKERS = {"paper", "eastmoney_stub"}
SUPPORTED_MARKET_PROVIDERS = {"ifind", "dsa"}


def _path_status(path: Path) -> dict[str, Any]:
    parent = path.parent
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "parent_exists": parent.exists(),
    }


def _strategy_summary(strategies: list[dict[str, Any]], errors: list[str], warnings: list[str]) -> dict[str, Any]:
    ids: list[str] = []
    symbols: list[str] = []
    seen_ids: set[str] = set()
    enabled_count = 0

    for index, strategy in enumerate(strategies):
        if not isinstance(strategy, dict):
            errors.append(f"strategy[{index}] must be an object")
            continue

        strategy_id = str(strategy.get("id") or "").strip()
        symbol = str(strategy.get("symbol") or "").strip()
        strategy_type = str(strategy.get("strategy") or "").strip()
        enabled = bool(strategy.get("enabled", True))

        if enabled:
            enabled_count += 1
        if strategy_id:
            ids.append(strategy_id)
            if strategy_id in seen_ids:
                errors.append(f"duplicate strategy id: {strategy_id}")
            seen_ids.add(strategy_id)
        else:
            errors.append(f"strategy[{index}] missing id")

        if symbol:
            symbols.append(symbol)
        else:
            errors.append(f"strategy[{index}] missing symbol")

        if not strategy_type:
            errors.append(f"strategy[{index}] missing strategy")

    if strategies and enabled_count == 0:
        warnings.append("no enabled strategy")

    return {
        "count": len(strategies),
        "enabled_count": enabled_count,
        "disabled_count": max(0, len(strategies) - enabled_count),
        "ids": ids,
        "symbols": sorted(set(symbols)),
    }


def _safe_broker_status(status: dict[str, Any] | None) -> dict[str, Any]:
    if not status:
        return {}
    safe: dict[str, Any] = {}
    for key, value in status.items():
        lowered = key.lower()
        if any(secret_word in lowered for secret_word in ("token", "secret", "password")):
            safe[f"{key}Configured"] = bool(value)
            continue
        safe[key] = value
    return safe


def diagnose_config(config: AppConfig, broker_status: dict[str, Any] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if config.broker not in SUPPORTED_BROKERS:
        errors.append(f"unsupported broker: {config.broker}")
    if config.market_provider not in SUPPORTED_MARKET_PROVIDERS:
        errors.append(f"unsupported market provider: {config.market_provider}")

    if config.broker == "eastmoney_stub" and not config.eastmoney_account_id:
        warnings.append("TRADER_EASTMONEY_ACCOUNT_ID is not configured for eastmoney_stub")

    if config.market_provider == "ifind":
        if not (config.ifind_access_token or config.ifind_refresh_token):
            errors.append("IFIND_ACCESS_TOKEN or IFIND_REFRESH_TOKEN is required for ifind provider")
        elif config.ifind_access_token and not config.ifind_refresh_token:
            warnings.append("IFIND_REFRESH_TOKEN is not configured; access token refresh will be unavailable")
    elif config.market_provider == "dsa":
        worker_path = config.app_dir / "src" / "providers" / "dsa_worker.py"
        if not config.dsa_root.exists():
            errors.append(f"DSA root not found: {config.dsa_root}")
        if not worker_path.exists():
            errors.append(f"DSA worker not found: {worker_path}")
        if config.dsa_python_bin.strip() in {"", "python"}:
            warnings.append("TRADER_DSA_PYTHON_BIN uses current shell python; confirm DSA dependencies are installed")
        if not any(
            [
                config.dsa_enable_market_stats,
                config.dsa_enable_hot_stocks,
                config.dsa_enable_limit_up_pool,
                config.dsa_enable_sector_rankings,
                config.dsa_enable_concept_rankings,
                config.dsa_use_alphasift,
            ]
        ):
            warnings.append("DSA extended market scanners are disabled; market observation will use strategy snapshots only")

    strategies: list[dict[str, Any]] = []
    if not config.strategy_file.exists():
        errors.append(f"strategy file not found: {config.strategy_file}")
    else:
        try:
            strategies = load_strategy_config(config.strategy_file)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"failed to load strategy file: {exc}")

    strategy_summary = _strategy_summary(strategies, errors, warnings)

    runtime_dir = config.runtime_dir
    runtime_parent = runtime_dir.parent
    if runtime_dir.exists() and not runtime_dir.is_dir():
        errors.append(f"runtime path is not a directory: {runtime_dir}")
    if not runtime_dir.exists() and not runtime_parent.exists():
        warnings.append(f"runtime parent does not exist yet: {runtime_parent}")

    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "config": {
            "app_dir": str(config.app_dir),
            "env_file": str(config.env_file),
            "env_file_exists": config.env_file.exists(),
            "runtime_dir": str(config.runtime_dir),
            "strategy_file": str(config.strategy_file),
            "loop_interval_seconds": config.loop_interval_seconds,
            "log_level": config.log_level,
        },
        "data_source": {
            "provider": config.market_provider,
            "base_url": config.ifind_base_url,
            "access_token_configured": bool(config.ifind_access_token),
            "refresh_token_configured": bool(config.ifind_refresh_token),
            "timeout_ms": config.ifind_request_timeout_ms,
            "dsa_root": str(config.dsa_root),
            "dsa_root_exists": config.dsa_root.exists(),
            "dsa_python_bin": config.dsa_python_bin,
            "dsa_market": config.dsa_market,
            "dsa_use_alphasift": config.dsa_use_alphasift,
            "dsa_timeout_seconds": config.dsa_timeout_seconds,
            "dsa_screening_strategy": config.dsa_screening_strategy,
            "dsa_screening_max_results": config.dsa_screening_max_results,
            "dsa_enable_market_stats": config.dsa_enable_market_stats,
            "dsa_enable_hot_stocks": config.dsa_enable_hot_stocks,
            "dsa_enable_limit_up_pool": config.dsa_enable_limit_up_pool,
            "dsa_enable_sector_rankings": config.dsa_enable_sector_rankings,
            "dsa_enable_concept_rankings": config.dsa_enable_concept_rankings,
        },
        "broker": {
            "provider": config.broker,
            "supported": config.broker in SUPPORTED_BROKERS,
            "status": _safe_broker_status(broker_status),
        },
        "runtime": {
            "runtime_dir": _path_status(runtime_dir),
            "state_file": {
                "path": str(runtime_dir / "state.json"),
                "exists": (runtime_dir / "state.json").exists(),
            },
        },
        "strategies": strategy_summary,
    }
    return result
