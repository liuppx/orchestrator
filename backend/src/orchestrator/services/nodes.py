from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from orchestrator.models import RuntimeNodeHeartbeatRequest, RuntimeNodeListResponse, RuntimeNodeView
from orchestrator.storage import RuntimeNode, sessions_for


NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RuntimeNodeService:
    def __init__(self, database_url: str | None, offline_after_seconds: int = 90) -> None:
        if not database_url:
            raise RuntimeError("node heartbeat requires HUB_DATABASE_URL")
        self.sessions = sessions_for(database_url)
        self.offline_after_seconds = offline_after_seconds

    def heartbeat(self, node_id: str, report: RuntimeNodeHeartbeatRequest) -> RuntimeNodeView:
        normalized_id = node_id.strip()
        if not NODE_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError("invalid node id")

        now = datetime.now(timezone.utc)
        capacity = {
            "agent_version": report.agent_version,
            "providers": [provider.model_dump() for provider in report.providers],
            "resources": report.resources,
            "labels": report.labels,
        }
        try:
            with self.sessions.begin() as session:
                node = session.get(RuntimeNode, normalized_id, with_for_update=True)
                if node is None:
                    node = RuntimeNode(
                        id=normalized_id,
                        node_key=normalized_id,
                        status="online",
                        last_seen_at=now,
                        capacity=capacity,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(node)
                else:
                    node.status = "online"
                    node.last_seen_at = now
                    node.capacity = capacity
                    node.version += 1
                    node.updated_at = now
                session.flush()
                return self._view(node, now)
        except IntegrityError as exc:
            raise RuntimeError("node registration raced with another heartbeat; retry") from exc
        except SQLAlchemyError as exc:
            raise RuntimeError("runtime node storage is unavailable") from exc

    def list_nodes(self) -> RuntimeNodeListResponse:
        now = datetime.now(timezone.utc)
        try:
            with self.sessions() as session:
                rows = session.scalars(select(RuntimeNode).order_by(RuntimeNode.id.asc())).all()
                items = [self._view(row, now) for row in rows]
        except SQLAlchemyError as exc:
            raise RuntimeError("runtime node storage is unavailable") from exc
        return RuntimeNodeListResponse(
            items=items,
            total=len(items),
            online=sum(item.status == "online" for item in items),
            offline=sum(item.status == "offline" for item in items),
            unknown=sum(item.status == "unknown" for item in items),
        )

    def _view(self, node: RuntimeNode, now: datetime) -> RuntimeNodeView:
        last_seen = node.last_seen_at
        if last_seen is None:
            status = "unknown"
        else:
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age_seconds = max(0.0, (now - last_seen).total_seconds())
            status = "online" if age_seconds <= self.offline_after_seconds else "offline"

        capacity = node.capacity if isinstance(node.capacity, dict) else {}
        return RuntimeNodeView(
            id=node.id,
            status=status,
            last_seen_at=last_seen.isoformat() if last_seen else None,
            agent_version=str(capacity["agent_version"]) if capacity.get("agent_version") is not None else None,
            providers=capacity.get("providers", []),
            resources=capacity.get("resources", {}),
            labels=capacity.get("labels", {}),
        )
