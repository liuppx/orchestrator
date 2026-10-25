from __future__ import annotations

from pathlib import Path
from typing import Any

from hub.models import (
    AgentWorkspaceActionResponse,
    AgentWorkspaceConfigUpdateResponse,
    AgentWorkspaceSummaryResponse,
)
from hub.services.messenger import MessengerRuntimeConfig, MessengerStateService


class MessengerWorkspaceAdapter:
    def __init__(self, root_dir: Path, repo_root: Path) -> None:
        self.root_dir = root_dir
        self.repo_root = repo_root

    def exists(self) -> bool:
        return self.root_dir.exists()

    @property
    def runtime_dir(self) -> Path:
        return self.repo_root / "runtime" / "control-plane"

    @property
    def instances_root(self) -> Path:
        return self.repo_root / "runtime" / "instances"

    @property
    def state_file(self) -> Path:
        return self.runtime_dir / "state.json"

    def service(self) -> MessengerStateService:
        return MessengerStateService(
            MessengerRuntimeConfig(
                state_file=self.state_file,
                instances_root=self.instances_root,
                runtime_dir=self.runtime_dir,
                repo_root=self.repo_root,
                default_model="gpt-5.3-codex",
                model_allowlist=[],
                router_base_url="",
                router_api_key=None,
                port_range_start=18800,
                port_range_end=18999,
                openclaw_prefix=None,
            )
        )

    def summary(self) -> AgentWorkspaceSummaryResponse:
        if not self.exists():
            return AgentWorkspaceSummaryResponse(
                available=False,
                broker="unavailable",
                running=False,
                pid=None,
                runtime_dir=str(self.runtime_dir),
                strategy_file="",
                state_file=str(self.state_file),
                service_log_path="",
                strategies=[],
                state={},
                recent_signals=[],
                recent_orders=[],
                service_log_tail="",
            )

        listing = self.service().list_instances()
        items = [item.model_dump(mode="python") for item in listing.items]
        running_count = sum(1 for item in listing.items if item.status == "running")
        state: dict[str, Any] = {
            "defaultModel": listing.defaultModel,
            "instanceCount": len(listing.items),
            "runningCount": running_count,
            "items": items,
        }
        return AgentWorkspaceSummaryResponse(
            available=True,
            broker="multi-channel",
            running=running_count > 0,
            pid=None,
            runtime_dir=str(self.runtime_dir),
            strategy_file="",
            state_file=str(self.state_file),
            service_log_path="",
            strategies=[],
            state=state,
            recent_signals=[],
            recent_orders=[],
            service_log_tail="",
        )

    def update_config(self, broker: str, strategy: dict[str, Any]) -> AgentWorkspaceConfigUpdateResponse:
        _ = broker
        _ = strategy
        raise NotImplementedError("messenger workspace does not support config updates")

    def run_action(self, action: str) -> AgentWorkspaceActionResponse:
        _ = action
        raise NotImplementedError("messenger workspace does not support direct actions")
