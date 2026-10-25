from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hub.models import AgentListItem, AgentListResponse


@dataclass(slots=True)
class AgentDescriptor:
    key: str
    display_name: str
    root_dir: Path
    category: str = "custom"


class AgentRegistry:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def builtins(self) -> list[AgentDescriptor]:
        agents_root = self.repo_root / "agents" / "custom"
        return [
            AgentDescriptor("trader", "交易员", agents_root / "trader"),
            AgentDescriptor("messenger", "信使", agents_root / "messenger"),
        ]

    def list_items(self) -> AgentListResponse:
        items = [
            AgentListItem(
                key=agent.key,
                display_name=agent.display_name,
                category=agent.category,
                path=str(agent.root_dir),
                available=agent.root_dir.exists(),
            )
            for agent in self.builtins()
        ]
        return AgentListResponse(items=items)
