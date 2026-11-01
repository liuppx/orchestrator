from __future__ import annotations

import argparse
import time
from orchestrator.config import Settings

from .core import HubClient, HubClientConfig, load_hub_credential
from .http import HttpHubTransport, HubTransportError
from .openclaw_adapter import OpenClawAdapter
from .simulated import SimulatedAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Machine Hub client")
    parser.add_argument("--once", action="store_true", help="run one synchronization cycle and exit")
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
        if args.simulate or args.openclaw or args.once:
            raise SystemExit("--migrate-openclaw-state cannot be combined with runtime options")
        if not settings.hub_id:
            raise SystemExit("HUB_ID is required to select the Hub-local state directory")
        try:
            migrated = OpenClawAdapter.migrate_backend_state_to_local(settings)
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"OpenClaw state migration failed: {exc}") from exc
        print(f"copied {migrated} OpenClaw instance records to {settings.resolved_hub_openclaw_state_file}")
        return
    if not settings.hub_id:
        raise SystemExit("HUB_ID is required")
    if not settings.hub_backend_url:
        raise SystemExit("HUB_BACKEND_URL is required")
    if not args.simulate and not args.openclaw:
        raise SystemExit("Select at least one adapter: --openclaw or --simulate")
    token = load_hub_credential(settings.resolved_hub_credential_file)
    config = HubClientConfig.local(settings.hub_id, settings.hub_version, settings.resolved_hub_state_db)
    client = HubClient(
        config,
        HttpHubTransport(settings.hub_backend_url, settings.hub_id, token, settings.hub_request_timeout_seconds),
        adapters=[
            *([OpenClawAdapter.from_settings(settings)] if args.openclaw else []),
            *([SimulatedAdapter()] if args.simulate else []),
        ],
    )
    try:
        while True:
            try:
                result = client.run_once()
            except HubTransportError as exc:
                status = f"HTTP {exc.status_code}" if exc.status_code else "network/protocol error"
                if args.once:
                    raise SystemExit(f"Hub synchronization failed ({status}); local inbox/outbox is retained") from exc
                print(f"hub={settings.hub_id} synchronization_failed={status}; retrying", flush=True)
                time.sleep(5)
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
            time.sleep(interval)
    finally:
        client.close()


if __name__ == "__main__":
    main()
