from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from pathlib import Path

from orchestrator.config import Settings

from .core import HubClient, HubClientConfig, load_hub_credential
from .http import HttpHubTransport, HubTransportError
from .openclaw_adapter import OpenClawAdapter
from .simulated import SimulatedAdapter


def _validate_local_path(path: Path, *, directory: bool, private: bool = False) -> None:
    if path.exists():
        if directory and not path.is_dir():
            raise ValueError(f"Hub path is not a directory: {path}")
        if not directory and not path.is_file():
            raise ValueError(f"Hub path is not a regular file: {path}")
        required_access = os.R_OK | os.W_OK | (os.X_OK if directory else 0)
        if not os.access(path, required_access):
            raise ValueError(f"Hub path is not accessible by the current user: {path}")
        if private and path.stat().st_mode & 0o077:
            raise ValueError(f"Hub file must not be accessible to group or other users: {path}")
        return

    writable_parent = path if directory else path.parent
    while not writable_parent.exists():
        parent = writable_parent.parent
        if parent == writable_parent:
            raise ValueError(f"Hub path has no existing parent directory: {path}")
        writable_parent = parent
    if not writable_parent.is_dir() or not os.access(writable_parent, os.W_OK | os.X_OK):
        raise ValueError(f"Hub path parent is not writable by the current user: {path}")


def _check_openclaw(adapter: OpenClawAdapter) -> None:
    config = adapter.service.cfg
    for directory in (
        config.runtime_dir,
        config.instances_root,
        config.secret_store_dir or (config.runtime_dir / "secrets"),
        config.state_file.parent,
    ):
        _validate_local_path(directory, directory=True)
    _validate_local_path(config.state_file, directory=False, private=True)
    if not config.state_file.exists():
        return
    try:
        catalog = json.loads(config.state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"OpenClaw instance catalog is not readable JSON: {config.state_file}") from exc
    if not isinstance(catalog, dict) or not isinstance(catalog.get("instances", {}), dict):
        raise ValueError(f"OpenClaw instance catalog has an invalid shape: {config.state_file}")


def _check_configuration(settings: Settings, *, openclaw: bool, simulate: bool) -> None:
    if not settings.hub_id:
        raise ValueError("HUB_ID is required")
    if not settings.hub_backend_url:
        raise ValueError("HUB_BACKEND_URL is required")
    if settings.resolved_hub_state_db == Path(":memory:"):
        raise ValueError("HUB_CLIENT_STATE_DB must be a persistent local file")
    if not openclaw and not simulate:
        raise ValueError("Select at least one adapter: --openclaw or --simulate")

    HubClientConfig.local(settings.hub_id, settings.hub_version, settings.resolved_hub_state_db)
    credential_file = settings.resolved_hub_credential_file
    load_hub_credential(credential_file)
    _validate_local_path(settings.resolved_hub_state_db, directory=False, private=True)
    HttpHubTransport(
        settings.hub_backend_url,
        settings.hub_id,
        lambda: load_hub_credential(credential_file),
        settings.hub_request_timeout_seconds,
    )
    if openclaw:
        _check_openclaw(OpenClawAdapter.from_settings(settings))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Machine Hub client")
    parser.add_argument("--once", action="store_true", help="run one synchronization cycle and exit")
    parser.add_argument("--check", action="store_true", help="validate local Hub configuration without contacting Backend")
    parser.add_argument("--simulate", action="store_true", help="attach the mock Runtime Adapter for protocol testing")
    parser.add_argument("--openclaw", action="store_true", help="manage OpenClaw instances on this machine")
    parser.add_argument(
        "--migrate-openclaw-state",
        action="store_true",
        help="copy OpenClaw instance metadata and local secrets from Backend storage to Hub-local storage",
    )
    args = parser.parse_args()
    settings = Settings()
    if args.migrate_openclaw_state:
        if args.check or args.simulate or args.openclaw or args.once:
            raise SystemExit("--migrate-openclaw-state cannot be combined with runtime options")
        if not settings.hub_id:
            raise SystemExit("HUB_ID is required to select the Hub-local state directory")
        try:
            migrated = OpenClawAdapter.migrate_backend_state_to_local(settings)
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"OpenClaw state migration failed: {exc}") from exc
        print(f"copied {migrated} OpenClaw instance records to {settings.resolved_hub_openclaw_state_file}")
        return
    if settings.resolved_hub_state_db == Path(":memory:"):
        raise SystemExit("HUB_CLIENT_STATE_DB must be a persistent local file")
    if args.check:
        try:
            _check_configuration(settings, openclaw=args.openclaw, simulate=args.simulate)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Hub preflight failed: {exc}") from exc
        adapters = ",".join(name for name, enabled in (("openclaw", args.openclaw), ("simulate", args.simulate)) if enabled)
        print(f"Hub preflight passed: hub={settings.hub_id} adapters={adapters} credential=valid")
        return

    if not settings.hub_id:
        raise SystemExit("HUB_ID is required")
    if not settings.hub_backend_url:
        raise SystemExit("HUB_BACKEND_URL is required")
    if not args.simulate and not args.openclaw:
        raise SystemExit("Select at least one adapter: --openclaw or --simulate")
    config = HubClientConfig.local(settings.hub_id, settings.hub_version, settings.resolved_hub_state_db)
    client = HubClient(
        config,
        HttpHubTransport(
            settings.hub_backend_url,
            settings.hub_id,
            lambda: load_hub_credential(settings.resolved_hub_credential_file),
            settings.hub_request_timeout_seconds,
        ),
        adapters=[
            *([OpenClawAdapter.from_settings(settings)] if args.openclaw else []),
            *([SimulatedAdapter()] if args.simulate else []),
        ],
    )
    stop_requested = threading.Event()
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    def request_shutdown(_signum: int, _frame: object) -> None:
        stop_requested.set()
        client.request_shutdown()

    for signum in previous_handlers:
        signal.signal(signum, request_shutdown)
    try:
        while not stop_requested.is_set():
            try:
                result = client.run_once()
            except HubTransportError as exc:
                status = f"HTTP {exc.status_code}" if exc.status_code else "network/protocol error"
                if args.once:
                    raise SystemExit(f"Hub synchronization failed ({status}); local inbox/outbox is retained") from exc
                print(f"hub={settings.hub_id} synchronization_failed={status}; retrying", flush=True)
                stop_requested.wait(5)
                continue
            print(
                f"hub={settings.hub_id} inventory_revision={result.inventory_revision} "
                f"commands={result.polled_commands} executed={result.executed_commands} "
                f"pending_events={result.pending_events}",
                flush=True,
            )
            if args.once:
                return
            interval = result.heartbeat.command_poll_interval_seconds if result.heartbeat else 5
            stop_requested.wait(interval)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        client.close()


if __name__ == "__main__":
    main()
