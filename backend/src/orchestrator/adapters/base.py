from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from orchestrator.models import (
    AgentWorkspaceActionResponse,
    AgentWorkspaceConfigUpdateResponse,
    AgentWorkspaceSummaryResponse,
)


class AgentWorkspaceAdapter(Protocol):
    root_dir: Path

    def exists(self) -> bool: ...

    def summary(self) -> AgentWorkspaceSummaryResponse: ...

    def update_config(
        self,
        broker: str,
        strategy: dict[str, Any],
        strategy_id: str | None = None,
    ) -> AgentWorkspaceConfigUpdateResponse: ...

    def run_action(self, action: str) -> AgentWorkspaceActionResponse: ...
