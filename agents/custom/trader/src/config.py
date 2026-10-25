from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_env_file(path: Path) -> None:
    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


@dataclass
class AppConfig:
    app_dir: Path
    env_file: Path
    runtime_dir: Path
    strategy_file: Path
    loop_interval_seconds: int
    log_level: str
    broker: str
    market_provider: str
    ifind_base_url: str
    ifind_access_token: str | None
    ifind_refresh_token: str | None
    ifind_request_timeout_ms: int
    eastmoney_account_id: str | None
    dsa_root: Path
    dsa_python_bin: str
    dsa_market: str
    dsa_use_alphasift: bool
    dsa_timeout_seconds: int
    dsa_screening_strategy: str
    dsa_screening_max_results: int
    dsa_hot_stock_count: int
    dsa_limit_up_count: int
    dsa_concept_top_n: int
    dsa_sector_top_n: int
    dsa_history_days: int
    dsa_enable_market_stats: bool
    dsa_enable_hot_stocks: bool
    dsa_enable_limit_up_pool: bool
    dsa_enable_sector_rankings: bool
    dsa_enable_concept_rankings: bool


def load_strategy_config(path: Path) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    strategies = payload.get("strategies") or []
    if not isinstance(strategies, list):
        raise ValueError("strategies must be a list")
    return strategies


def load_app_config(app_dir: Path, env_file: Path | None = None) -> AppConfig:
    env_file = env_file or app_dir / "config" / "trader.env"
    load_env_file(env_file)

    runtime_dir = Path(os.environ.get("TRADER_RUNTIME_DIR", "runtime"))
    if not runtime_dir.is_absolute():
        runtime_dir = app_dir / runtime_dir

    strategy_file = Path(os.environ.get("TRADER_STRATEGY_FILE", "config/strategies.yaml"))
    if not strategy_file.is_absolute():
        strategy_file = app_dir / strategy_file

    return AppConfig(
        app_dir=app_dir,
        env_file=env_file,
        runtime_dir=runtime_dir,
        strategy_file=strategy_file,
        loop_interval_seconds=int(os.environ.get("TRADER_LOOP_INTERVAL_SECONDS", "300")),
        log_level=os.environ.get("TRADER_LOG_LEVEL", "INFO"),
        broker=os.environ.get("TRADER_BROKER", "paper").strip().lower(),
        market_provider=os.environ.get("TRADER_MARKET_PROVIDER", "dsa").strip().lower(),
        ifind_base_url=os.environ.get("IFIND_BASE_URL", "https://quantapi.51ifind.com/api/v1").rstrip("/"),
        ifind_access_token=os.environ.get("IFIND_ACCESS_TOKEN") or None,
        ifind_refresh_token=os.environ.get("IFIND_REFRESH_TOKEN") or None,
        ifind_request_timeout_ms=int(os.environ.get("IFIND_REQUEST_TIMEOUT_MS", "30000")),
        eastmoney_account_id=os.environ.get("TRADER_EASTMONEY_ACCOUNT_ID") or None,
        dsa_root=Path(
            os.environ.get(
                "TRADER_DSA_ROOT",
                app_dir.parent.parent.parent.parent / "daily_stock_analysis",
            )
        ).expanduser(),
        dsa_python_bin=os.environ.get("TRADER_DSA_PYTHON_BIN", "python"),
        dsa_market=os.environ.get("TRADER_DSA_MARKET", "cn").strip().lower(),
        dsa_use_alphasift=os.environ.get("TRADER_DSA_USE_ALPHASIFT", "false").strip().lower() in {"1", "true", "yes", "on"},
        dsa_timeout_seconds=int(os.environ.get("TRADER_DSA_TIMEOUT_SECONDS", "120")),
        dsa_screening_strategy=os.environ.get("TRADER_DSA_SCREENING_STRATEGY", "dual_low").strip(),
        dsa_screening_max_results=int(os.environ.get("TRADER_DSA_SCREENING_MAX_RESULTS", "12")),
        dsa_hot_stock_count=int(os.environ.get("TRADER_DSA_HOT_STOCK_COUNT", "10")),
        dsa_limit_up_count=int(os.environ.get("TRADER_DSA_LIMIT_UP_COUNT", "12")),
        dsa_concept_top_n=int(os.environ.get("TRADER_DSA_CONCEPT_TOP_N", "5")),
        dsa_sector_top_n=int(os.environ.get("TRADER_DSA_SECTOR_TOP_N", "5")),
        dsa_history_days=int(os.environ.get("TRADER_DSA_HISTORY_DAYS", "40")),
        dsa_enable_market_stats=os.environ.get("TRADER_DSA_ENABLE_MARKET_STATS", "false").strip().lower() in {"1", "true", "yes", "on"},
        dsa_enable_hot_stocks=os.environ.get("TRADER_DSA_ENABLE_HOT_STOCKS", "false").strip().lower() in {"1", "true", "yes", "on"},
        dsa_enable_limit_up_pool=os.environ.get("TRADER_DSA_ENABLE_LIMIT_UP_POOL", "false").strip().lower() in {"1", "true", "yes", "on"},
        dsa_enable_sector_rankings=os.environ.get("TRADER_DSA_ENABLE_SECTOR_RANKINGS", "false").strip().lower() in {"1", "true", "yes", "on"},
        dsa_enable_concept_rankings=os.environ.get("TRADER_DSA_ENABLE_CONCEPT_RANKINGS", "false").strip().lower() in {"1", "true", "yes", "on"},
    )


def dump_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
