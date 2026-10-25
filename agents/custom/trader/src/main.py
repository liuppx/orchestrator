from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .brokers.eastmoney_stub import EastMoneyStubBroker
from .brokers.paper import PaperBroker
from .config import AppConfig, load_app_config, load_strategy_config
from .data.ifind_client import IfindClient, IfindSettings
from .diagnostics import SUPPORTED_BROKERS, diagnose_config
from .execution.ledger import unlock_order_guard
from .execution.replay import replay_run
from .execution.runner import run_cycle
from .providers import DsaBridge
from .providers.dsa_bridge import DsaBridgeConfig
from .scheduler import run_forever
from .state_store import StateStore


def build_logger(runtime_dir: Path, level: str) -> logging.Logger:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_file = runtime_dir / "logs" / "service.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("trader-bot")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def build_broker(config: AppConfig):
    if config.broker == "eastmoney_stub":
        return EastMoneyStubBroker(config.eastmoney_account_id)
    if config.broker == "paper":
        return PaperBroker()
    supported = ", ".join(sorted(SUPPORTED_BROKERS))
    raise ValueError(f"unsupported broker: {config.broker}; supported brokers: {supported}")


def run_once(config: AppConfig) -> dict:
    logger = build_logger(config.runtime_dir, config.log_level)
    if not config.strategy_file.exists():
        raise FileNotFoundError(f"strategy file not found: {config.strategy_file}")
    strategies = load_strategy_config(config.strategy_file)
    market_client: object
    if config.market_provider == "dsa":
        market_client = DsaBridge(
            DsaBridgeConfig(
                root_dir=config.dsa_root,
                python_bin=config.dsa_python_bin,
                market=config.dsa_market,
                use_alphasift=config.dsa_use_alphasift,
                timeout_seconds=config.dsa_timeout_seconds,
                screening_strategy=config.dsa_screening_strategy,
                screening_max_results=config.dsa_screening_max_results,
                hot_stock_count=config.dsa_hot_stock_count,
                limit_up_count=config.dsa_limit_up_count,
                concept_top_n=config.dsa_concept_top_n,
                sector_top_n=config.dsa_sector_top_n,
                history_days=config.dsa_history_days,
                enable_market_stats=config.dsa_enable_market_stats,
                enable_hot_stocks=config.dsa_enable_hot_stocks,
                enable_limit_up_pool=config.dsa_enable_limit_up_pool,
                enable_sector_rankings=config.dsa_enable_sector_rankings,
                enable_concept_rankings=config.dsa_enable_concept_rankings,
            ),
            trader_root=config.app_dir,
        )
    else:
        market_client = IfindClient(
            IfindSettings(
                base_url=config.ifind_base_url,
                access_token=config.ifind_access_token,
                refresh_token=config.ifind_refresh_token,
                timeout_ms=config.ifind_request_timeout_ms,
            )
        )
    state_store = StateStore(config.runtime_dir)
    broker = build_broker(config)
    result = run_cycle(
        strategies=strategies,
        ifind_client=market_client,
        broker=broker,
        runtime_dir=config.runtime_dir,
        state_store=state_store,
        logger=logger,
    )
    logger.info("cycle result=%s", json.dumps(result, ensure_ascii=True))
    return result


def replay_once(config: AppConfig, run_id: str) -> dict:
    logger = build_logger(config.runtime_dir, config.log_level)
    if not config.strategy_file.exists():
        raise FileNotFoundError(f"strategy file not found: {config.strategy_file}")
    strategies = load_strategy_config(config.strategy_file)
    result = replay_run(
        runtime_dir=config.runtime_dir,
        source_run_id=run_id,
        strategies=strategies,
        logger=logger,
    )
    logger.info("replay result=%s", json.dumps(result, ensure_ascii=True))
    return result


def diagnose_once(config: AppConfig) -> dict:
    broker_status = None
    try:
        broker_status = build_broker(config).broker_status()
    except ValueError:
        broker_status = None
    result = diagnose_config(config, broker_status=broker_status)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def init_config_once(config: AppConfig) -> dict:
    files = [
        {
            "kind": "env",
            "source": config.app_dir / "config" / "trader.env.template",
            "target": config.env_file,
        },
        {
            "kind": "strategy",
            "source": config.app_dir / "config" / "strategies.example.yaml",
            "target": config.strategy_file,
        },
    ]
    results = []
    errors = []
    for item in files:
        source = item["source"]
        target = item["target"]
        if target.exists():
            results.append({**item, "source": str(source), "target": str(target), "status": "exists"})
            continue
        if not source.exists():
            errors.append(f"template not found: {source}")
            results.append({**item, "source": str(source), "target": str(target), "status": "missing_template"})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        results.append({**item, "source": str(source), "target": str(target), "status": "created"})

    result = {
        "ok": not errors,
        "errors": errors,
        "files": results,
    }
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def unlock_order_once(
    config: AppConfig,
    *,
    trading_day: str,
    strategy_id: str,
    symbol: str,
    side: str,
    reason: str,
    actor: str,
) -> dict:
    logger = build_logger(config.runtime_dir, config.log_level)
    result = unlock_order_guard(
        runtime_dir=config.runtime_dir,
        trading_day=trading_day,
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        reason=reason,
        actor=actor,
    )
    logger.info("unlock order guard result=%s", json.dumps(result, ensure_ascii=True))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["run-once", "run-loop", "replay", "unlock-order", "diagnose", "init-config"],
        nargs="?",
        default="run-once",
    )
    parser.add_argument("--env-file", dest="env_file")
    parser.add_argument("--run-id", dest="run_id")
    parser.add_argument("--trading-day", dest="trading_day")
    parser.add_argument("--strategy-id", dest="strategy_id")
    parser.add_argument("--symbol", dest="symbol")
    parser.add_argument("--side", dest="side")
    parser.add_argument("--reason", dest="reason")
    parser.add_argument("--actor", dest="actor", default="manual")
    args = parser.parse_args()

    app_dir = Path(__file__).resolve().parent.parent
    env_file = Path(args.env_file).resolve() if args.env_file else None
    config = load_app_config(app_dir, env_file)

    if args.command == "run-once":
        run_once(config)
        return

    if args.command == "replay":
        if not args.run_id:
            parser.error("--run-id is required for replay")
        replay_once(config, args.run_id)
        return

    if args.command == "diagnose":
        result = diagnose_once(config)
        if not result.get("ok"):
            sys.exit(1)
        return

    if args.command == "init-config":
        result = init_config_once(config)
        if not result.get("ok"):
            sys.exit(1)
        return

    if args.command == "unlock-order":
        missing = [
            name
            for name, value in {
                "--trading-day": args.trading_day,
                "--strategy-id": args.strategy_id,
                "--symbol": args.symbol,
                "--side": args.side,
                "--reason": args.reason,
            }.items()
            if not value
        ]
        if missing:
            parser.error(f"{', '.join(missing)} required for unlock-order")
        unlock_order_once(
            config,
            trading_day=args.trading_day,
            strategy_id=args.strategy_id,
            symbol=args.symbol,
            side=args.side,
            reason=args.reason,
            actor=args.actor,
        )
        return

    logger = build_logger(config.runtime_dir, config.log_level)

    def cycle() -> None:
        run_once(config)

    run_forever(config.loop_interval_seconds, cycle, logger)


if __name__ == "__main__":
    main()
