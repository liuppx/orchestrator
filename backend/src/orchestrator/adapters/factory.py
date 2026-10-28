from __future__ import annotations

from pathlib import Path
from typing import Callable

from orchestrator.adapters.base import AgentWorkspaceAdapter
from orchestrator.adapters.messenger import MessengerWorkspaceAdapter
from orchestrator.adapters.trader import TraderAdapter

WorkspaceAdapterFactory = Callable[[Path, Path], AgentWorkspaceAdapter]


WORKSPACE_ADAPTER_FACTORIES: dict[str, WorkspaceAdapterFactory] = {
    "trader": lambda root_dir, _repo_root: TraderAdapter(root_dir),
    "messenger": lambda root_dir, repo_root: MessengerWorkspaceAdapter(root_dir, repo_root),
}


def builtin_agent_root(repo_root: Path, agent_key: str) -> Path | None:
    custom_root = repo_root / "agents" / "custom"
    roots = {
        "trader": custom_root / "trader",
        "messenger": custom_root / "messenger",
    }
    return roots.get(agent_key)


def get_agent_workspace_adapter(repo_root: Path, agent_key: str) -> AgentWorkspaceAdapter | None:
    agent_root = builtin_agent_root(repo_root, agent_key)
    if agent_root is None:
        return None
    factory = WORKSPACE_ADAPTER_FACTORIES.get(agent_key)
    if factory is None:
        return None
    return factory(agent_root, repo_root)
