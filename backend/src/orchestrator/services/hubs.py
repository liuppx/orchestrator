from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from orchestrator.protocols.hub_v1 import (
    HubCommandEventV1,
    HubCommandIssueRequestV1,
    HubCommandLeaseV1,
    HubCommandOperation,
    HubCommandPollResponseV1,
    HubCommandReceiptV1,
    HubCommandV1,
    HubCredentialIssuedV1,
    HubCredentialViewV1,
    HubEventBatchAckV1,
    HubEventBatchRequestV1,
    HubHeartbeatRequestV1,
    HubHeartbeatResponseV1,
    HubInventoryAckV1,
    HubInventorySnapshotV1,
    expected_result_kind,
)
from orchestrator.storage import (
    AgentInstance,
    AuditEvent,
    InstanceConfig,
    MachineHub,
    MachineHubCommand,
    MachineHubCredential,
    MachineHubEvent,
    RuntimeNode,
    sessions_for,
)


class HubServiceError(RuntimeError):
    pass


class HubNotFoundError(HubServiceError):
    pass


class HubConflictError(HubServiceError):
    pass


class HubAuthenticationError(HubServiceError):
    pass


ACTIVE_CREDENTIAL_LIMIT = 3
COMMAND_LEASE_SECONDS = 60
MAX_COMMAND_TTL = timedelta(hours=24)
TERMINAL_COMMAND_PHASES = {"succeeded", "failed", "rejected", "unknown", "expired", "superseded"}
STATEFUL_COMMAND_OPERATIONS = {"start_instance", "stop_instance", "restart_instance"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MachineHubService:
    def __init__(self, database_url: str | None) -> None:
        if not database_url:
            raise RuntimeError("Hub protocol requires HUB_DATABASE_URL")
        self.sessions = sessions_for(database_url)

    def issue_credential(self, hub_id: str, expires_in_seconds: int) -> HubCredentialIssuedV1:
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(48)
        credential_id = str(uuid4())
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    hub = MachineHub(
                        id=hub_id,
                        status="pending",
                        protocol_version="1.0",
                        metadata_json={},
                        inventory_revision=0,
                        inventory_json={},
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(hub)
                    session.flush()
                session.query(MachineHubCredential).filter(
                    MachineHubCredential.hub_id == hub_id,
                    MachineHubCredential.status == "active",
                    MachineHubCredential.expires_at <= now,
                ).update({MachineHubCredential.status: "expired"}, synchronize_session=False)
                active_count = session.scalar(
                    select(func.count())
                    .select_from(MachineHubCredential)
                    .where(MachineHubCredential.hub_id == hub_id, MachineHubCredential.status == "active")
                ) or 0
                if active_count >= ACTIVE_CREDENTIAL_LIMIT:
                    raise HubConflictError("at most three active Hub credentials are allowed")
                session.add(
                    MachineHubCredential(
                        id=credential_id,
                        hub_id=hub_id,
                        token_digest=_digest(token),
                        status="active",
                        expires_at=now + timedelta(seconds=expires_in_seconds),
                        created_at=now,
                    )
                )
                session.add(
                    AuditEvent(
                        id=str(uuid4()),
                        actor="hub-admin-token",
                        action="hub.credential.issue",
                        resource=f"hub/{hub_id}/credentials/{credential_id}",
                        outcome="succeeded",
                        metadata_json={"expires_at": (now + timedelta(seconds=expires_in_seconds)).isoformat()},
                        created_at=now,
                    )
                )
        except HubConflictError:
            raise
        except IntegrityError as exc:
            raise HubConflictError("Hub registration raced with another request; retry") from exc
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub credential storage is unavailable") from exc

        return HubCredentialIssuedV1(
            credential_id=credential_id,
            hub_id=hub_id,
            token=token,
            expires_at=now + timedelta(seconds=expires_in_seconds),
        )

    def revoke_credential(self, hub_id: str, credential_id: str) -> None:
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                credential = session.get(MachineHubCredential, credential_id, with_for_update=True)
                if credential is None or credential.hub_id != hub_id:
                    raise HubNotFoundError("Hub credential not found")
                if credential.status == "revoked":
                    return
                credential.status = "revoked"
                credential.revoked_at = now
                session.add(
                    AuditEvent(
                        id=str(uuid4()),
                        actor="hub-admin-token",
                        action="hub.credential.revoke",
                        resource=f"hub/{hub_id}/credentials/{credential_id}",
                        outcome="succeeded",
                        metadata_json={},
                        created_at=now,
                    )
                )
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub credential storage is unavailable") from exc

    def list_credentials(self, hub_id: str) -> list[HubCredentialViewV1]:
        now = datetime.now(UTC)
        try:
            with self.sessions() as session:
                if session.get(MachineHub, hub_id) is None:
                    raise HubNotFoundError("Hub not found")
                rows = session.scalars(
                    select(MachineHubCredential)
                    .where(MachineHubCredential.hub_id == hub_id)
                    .order_by(MachineHubCredential.created_at.desc())
                ).all()
                return [
                    HubCredentialViewV1(
                        credential_id=row.id,
                        status=(
                            "expired"
                            if row.status == "active" and _utc(row.expires_at) <= now
                            else row.status
                        ),
                        created_at=_utc(row.created_at),
                        expires_at=_utc(row.expires_at),
                        last_used_at=_utc(row.last_used_at) if row.last_used_at else None,
                        revoked_at=_utc(row.revoked_at) if row.revoked_at else None,
                    )
                    for row in rows
                ]
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub credential storage is unavailable") from exc

    def authenticate(self, hub_id: str, token: str | None) -> None:
        if not token:
            raise HubAuthenticationError("invalid Hub credentials")
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                credential = session.scalar(
                    select(MachineHubCredential).where(
                        MachineHubCredential.hub_id == hub_id,
                        MachineHubCredential.token_digest == _digest(token),
                    )
                )
                if credential is None or credential.status != "active" or _utc(credential.expires_at) <= now:
                    raise HubAuthenticationError("invalid Hub credentials")
                hub = session.get(MachineHub, hub_id)
                if hub is None:
                    raise HubAuthenticationError("invalid Hub credentials")
                credential.last_used_at = now
                hub.status = "online"
                hub.last_seen_at = now
                hub.updated_at = now
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub authentication storage is unavailable") from exc

    def heartbeat(self, hub_id: str, report: HubHeartbeatRequestV1) -> HubHeartbeatResponseV1:
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                hub.status = "online"
                hub.protocol_version = report.protocol_version
                hub.metadata_json = report.model_dump(mode="json")
                hub.last_seen_at = now
                hub.updated_at = now
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub heartbeat storage is unavailable") from exc
        return HubHeartbeatResponseV1(
            protocol_version="1.0",
            server_time=now,
            heartbeat_interval_seconds=30,
            command_poll_interval_seconds=5,
        )

    def update_inventory(self, hub_id: str, snapshot: HubInventorySnapshotV1) -> HubInventoryAckV1:
        now = datetime.now(UTC)
        payload = snapshot.model_dump(mode="json")
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                if snapshot.revision < hub.inventory_revision:
                    raise HubConflictError("inventory revision is stale")
                if snapshot.revision == hub.inventory_revision:
                    if payload != hub.inventory_json:
                        raise HubConflictError("inventory revision was reused with different content")
                else:
                    hub.inventory_revision = snapshot.revision
                    hub.inventory_json = payload
                for instance in snapshot.instances:
                    catalog_instance = session.get(AgentInstance, instance.instance_id, with_for_update=True)
                    if catalog_instance is None or catalog_instance.runtime_type != instance.runtime_type:
                        continue
                    catalog_instance.name = instance.display_name or catalog_instance.name
                    catalog_instance.kind = instance.workload_type or catalog_instance.kind
                    if instance.port is not None:
                        catalog_instance.port = instance.port
                    catalog_instance.pid = instance.process_id
                    catalog_instance.observed_state = instance.observed_state.value
                    if instance.model:
                        config = dict(catalog_instance.config_json or {})
                        config["model"] = instance.model
                        catalog_instance.config_json = config
                    catalog_instance.updated_at = now
                hub.status = "online"
                hub.last_seen_at = now
                hub.updated_at = now
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub inventory storage is unavailable") from exc
        return HubInventoryAckV1(protocol_version="1.0", accepted_revision=snapshot.revision, accepted_at=now)

    def issue_command(
        self,
        hub_id: str,
        request: HubCommandIssueRequestV1,
        actor: str = "hub-admin-token",
        catalog_owner_id: str | None = None,
    ) -> HubCommandReceiptV1:
        now = datetime.now(UTC)
        expires_at = _utc(request.expires_at)
        if expires_at <= now or expires_at > now + MAX_COMMAND_TTL:
            raise HubConflictError("command expiry must be in the next 24 hours")
        command_id = str(uuid4())
        requested_payload = request.create_payload or request.operation_payload
        try:
            with self.sessions.begin() as session:
                existing = session.scalar(
                    select(MachineHubCommand)
                    .where(
                        MachineHubCommand.hub_id == hub_id,
                        MachineHubCommand.idempotency_key == request.idempotency_key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    same_request = (
                        existing.target_instance_id == request.target_instance_id
                        and existing.runtime_type == request.runtime_type
                        and existing.operation == request.operation.value
                        and existing.desired_revision == request.desired_revision
                        and existing.trace_id == request.trace_id
                        and _utc(existing.expires_at) == expires_at
                        and (existing.payload_json or {})
                        == (requested_payload.model_dump(mode="json") if requested_payload else {})
                    )
                    if not same_request:
                        raise HubConflictError("command idempotency key was reused with a different request")
                    return HubCommandReceiptV1(
                        protocol_version="1.0",
                        command_id=existing.id,
                        status=existing.status,
                        issued_at=_utc(existing.issued_at),
                        expires_at=_utc(existing.expires_at),
                    )
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                adapters = (hub.metadata_json or {}).get("adapters", [])
                adapter = next((item for item in adapters if item.get("runtime_type") == request.runtime_type), None)
                if adapter is None:
                    raise HubConflictError("Hub has not reported the requested Runtime Adapter")
                if request.operation.value not in adapter.get("capabilities", []):
                    raise HubConflictError("Hub Adapter does not support the requested operation")
                instances = (hub.inventory_json or {}).get("instances", [])
                instance = next((item for item in instances if item.get("instance_id") == request.target_instance_id), None)
                if request.operation == HubCommandOperation.CREATE_INSTANCE:
                    if catalog_owner_id is None:
                        raise HubConflictError("instance creation requires an authenticated catalog owner")
                    if instance is not None or session.get(AgentInstance, request.target_instance_id) is not None:
                        raise HubConflictError("target Runtime Instance already exists")
                    node = session.get(RuntimeNode, hub_id, with_for_update=True)
                    if node is None:
                        node = RuntimeNode(
                            id=hub_id,
                            node_key=hub_id,
                            status="online",
                            capacity={},
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(node)
                        session.flush()
                    create_payload = request.create_payload
                    assert create_payload is not None
                    session.add(
                        AgentInstance(
                            id=request.target_instance_id,
                            owner_id=catalog_owner_id,
                            node_id=hub_id,
                            runtime_type=request.runtime_type,
                            profile_ref=f"openclaw-profile://{request.target_instance_id}",
                            workspace_ref=f"hub://{hub_id}/instances/{request.target_instance_id}/workspace",
                            kind=create_payload.kind,
                            name=create_payload.name,
                            port=None,
                            desired_state="stopped",
                            observed_state="unknown",
                            revision=request.desired_revision,
                            config_json={"model": create_payload.model, "template": create_payload.template},
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    if instance is None or instance.get("runtime_type") != request.runtime_type:
                        raise HubConflictError("target Runtime Instance is not present in the Hub inventory")
                    if request.operation.value not in instance.get("capabilities", []):
                        raise HubConflictError("Runtime Instance does not support the requested operation")
                if request.operation.value in STATEFUL_COMMAND_OPERATIONS:
                    latest_state_command = session.scalar(
                        select(MachineHubCommand)
                        .where(
                            MachineHubCommand.hub_id == hub_id,
                            MachineHubCommand.target_instance_id == request.target_instance_id,
                            MachineHubCommand.operation.in_(STATEFUL_COMMAND_OPERATIONS),
                        )
                        .order_by(
                            MachineHubCommand.desired_revision.desc(),
                            MachineHubCommand.created_at.desc(),
                        )
                        .limit(1)
                        .with_for_update()
                    )
                    if latest_state_command is not None and request.desired_revision <= latest_state_command.desired_revision:
                        raise HubConflictError(
                            "state command desired_revision must increase; retry with its original idempotency key"
                        )
                    if latest_state_command is not None:
                        outdated_commands = session.scalars(
                            select(MachineHubCommand)
                            .where(
                                MachineHubCommand.hub_id == hub_id,
                                MachineHubCommand.target_instance_id == request.target_instance_id,
                                MachineHubCommand.desired_revision < request.desired_revision,
                                MachineHubCommand.status.in_(["pending", "running"]),
                            )
                            .with_for_update()
                        ).all()
                        for outdated in outdated_commands:
                            outdated.status = "superseded"
                            outdated.updated_at = now
                    catalog_instance = session.get(AgentInstance, request.target_instance_id, with_for_update=True)
                    if catalog_instance is not None and catalog_instance.runtime_type == request.runtime_type:
                        catalog_instance.desired_state = (
                            "running" if request.operation.value in {"start_instance", "restart_instance"} else "stopped"
                        )
                        catalog_instance.revision = request.desired_revision
                        catalog_instance.updated_at = now
                row = MachineHubCommand(
                    id=command_id,
                    hub_id=hub_id,
                    target_instance_id=request.target_instance_id,
                    runtime_type=request.runtime_type,
                    operation=request.operation.value,
                    idempotency_key=request.idempotency_key,
                    desired_revision=request.desired_revision,
                    attempt=0,
                    trace_id=request.trace_id,
                    issued_at=now,
                    expires_at=expires_at,
                    status="pending",
                    payload_json=(requested_payload.model_dump(mode="json") if requested_payload else None),
                    result_json={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.add(
                    AuditEvent(
                        id=str(uuid4()),
                        actor=actor,
                        action="hub.command.issue",
                        resource=f"hub/{hub_id}/commands/{command_id}",
                        outcome="succeeded",
                        request_id=request.trace_id,
                        metadata_json={
                            "target_instance_id": request.target_instance_id,
                            "runtime_type": request.runtime_type,
                            "operation": request.operation.value,
                            "desired_revision": request.desired_revision,
                        },
                        created_at=now,
                    )
                )
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command storage is unavailable") from exc
        return HubCommandReceiptV1(
            protocol_version="1.0",
            command_id=command_id,
            status="pending",
            issued_at=now,
            expires_at=expires_at,
        )

    def next_desired_revision(self, hub_id: str, instance_id: str) -> int:
        try:
            with self.sessions() as session:
                hub_exists = session.get(MachineHub, hub_id) is not None
                if not hub_exists:
                    raise HubNotFoundError("Hub not found")
                current = session.scalar(
                    select(func.max(MachineHubCommand.desired_revision)).where(
                        MachineHubCommand.hub_id == hub_id,
                        MachineHubCommand.target_instance_id == instance_id,
                        MachineHubCommand.operation.in_(STATEFUL_COMMAND_OPERATIONS),
                    )
                )
                return int(current or 0) + 1
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command storage is unavailable") from exc

    def instance_hub_id(self, instance_id: str) -> str | None:
        """Return the catalog owner Hub for routing an authorized instance operation."""
        try:
            with self.sessions() as session:
                instance = session.get(AgentInstance, instance_id)
                return instance.node_id if instance is not None else None
        except SQLAlchemyError as exc:
            raise RuntimeError("instance catalog storage is unavailable") from exc

    def runtime_instance_metadata(self, instance_id: str) -> dict[str, object] | None:
        """Return the Hub and most recent command mirror for a catalog instance."""
        try:
            with self.sessions() as session:
                instance = session.get(AgentInstance, instance_id)
                if instance is None:
                    return None
                hub = session.get(MachineHub, instance.node_id)
                latest = session.scalar(
                    select(MachineHubCommand)
                    .where(
                        MachineHubCommand.hub_id == instance.node_id,
                        MachineHubCommand.target_instance_id == instance_id,
                    )
                    .order_by(MachineHubCommand.created_at.desc())
                    .limit(1)
                )
                return {
                    "hub_id": instance.node_id,
                    "hub_status": hub.status if hub is not None else "unknown",
                    "hub_last_seen_at": _utc(hub.last_seen_at).isoformat() if hub and hub.last_seen_at else None,
                    "latest_command_status": latest.status if latest is not None else None,
                    "latest_command_operation": latest.operation if latest is not None else None,
                    "latest_command_updated_at": _utc(latest.updated_at).isoformat() if latest is not None else None,
                }
        except SQLAlchemyError as exc:
            raise RuntimeError("instance catalog storage is unavailable") from exc

    def list_runtime_hubs(self, owner_id: str | None = None) -> list[dict[str, object]]:
        try:
            with self.sessions() as session:
                hub_query = select(MachineHub).order_by(MachineHub.id.asc())
                if owner_id is not None:
                    hub_query = hub_query.join(AgentInstance, AgentInstance.node_id == MachineHub.id).where(
                        AgentInstance.owner_id == owner_id
                    ).distinct()
                hubs = session.scalars(hub_query).all()
                counts = {
                    hub_id: count
                    for hub_id, count in session.execute(
                        select(AgentInstance.node_id, func.count()).group_by(AgentInstance.node_id)
                    ).all()
                }
                return [
                    {
                        "id": hub.id,
                        "status": hub.status,
                        "last_seen_at": _utc(hub.last_seen_at).isoformat() if hub.last_seen_at else None,
                        "adapter_count": len((hub.metadata_json or {}).get("adapters", [])),
                        "instance_count": int(counts.get(hub.id, 0)),
                    }
                    for hub in hubs
                ]
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub catalog storage is unavailable") from exc

    def is_online(self, hub_id: str, offline_after_seconds: int = 90) -> bool:
        now = datetime.now(UTC)
        try:
            with self.sessions() as session:
                hub = session.get(MachineHub, hub_id)
                if hub is None or hub.last_seen_at is None or hub.status != "online":
                    return False
                return (now - _utc(hub.last_seen_at)).total_seconds() <= offline_after_seconds
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub status storage is unavailable") from exc

    def adapter_capabilities(self, hub_id: str, runtime_type: str) -> list[str]:
        try:
            with self.sessions() as session:
                hub = session.get(MachineHub, hub_id)
                if hub is None or hub.status != "online" or hub.last_seen_at is None:
                    return []
                if (datetime.now(UTC) - _utc(hub.last_seen_at)).total_seconds() > 90:
                    return []
                adapters = (hub.metadata_json or {}).get("adapters", [])
                adapter = next((item for item in adapters if item.get("runtime_type") == runtime_type), None)
                if adapter is None:
                    return []
                return list(adapter.get("capabilities", []))
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub adapter storage is unavailable") from exc

    def command_status(self, hub_id: str, command_id: str) -> dict | None:
        try:
            with self.sessions() as session:
                row = session.get(MachineHubCommand, command_id)
                if row is None or row.hub_id != hub_id:
                    return None
                return {
                    "status": row.status,
                    "result": dict(row.result_json or {}),
                    "updated_at": _utc(row.updated_at),
                }
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command storage is unavailable") from exc

    def command_status_summary(self, hub_id: str, command_id: str) -> dict | None:
        """Return an admin-safe status view without command payloads or results."""
        try:
            with self.sessions() as session:
                row = session.get(MachineHubCommand, command_id)
                if row is None or row.hub_id != hub_id:
                    return None
                stored_event = row.result_json
                event = stored_event if isinstance(stored_event, dict) else {}
                event_summary = {
                    key: event[key]
                    for key in (
                        "sequence",
                        "occurred_at",
                        "phase",
                        "progress_percent",
                        "observed_state",
                        "error_code",
                    )
                    if key in event
                }
                return {
                    "command_id": row.id,
                    "status": row.status,
                    "attempt": row.attempt,
                    "trace_id": row.trace_id,
                    "updated_at": _utc(row.updated_at),
                    "event": event_summary,
                }
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command storage is unavailable") from exc

    def claim_commands(self, hub_id: str, limit: int) -> HubCommandPollResponseV1:
        now = datetime.now(UTC)
        claimed: list[HubCommandV1] = []
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                rows = session.scalars(
                    select(MachineHubCommand)
                    .where(
                        MachineHubCommand.hub_id == hub_id,
                        MachineHubCommand.expires_at > now,
                        (MachineHubCommand.status == "pending")
                        | (
                            (MachineHubCommand.status == "running")
                            & (MachineHubCommand.lease_expires_at <= now)
                        ),
                    )
                    .order_by(MachineHubCommand.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).all()
                active_instances: set[str] = set()
                for row in rows:
                    if row.target_instance_id in active_instances:
                        continue
                    active_command_id = session.scalar(
                        select(MachineHubCommand.id)
                        .where(
                            MachineHubCommand.hub_id == hub_id,
                            MachineHubCommand.target_instance_id == row.target_instance_id,
                            MachineHubCommand.id != row.id,
                            MachineHubCommand.status.in_(["running", "superseded"]),
                            MachineHubCommand.lease_expires_at > now,
                        )
                        .limit(1)
                    )
                    if active_command_id is not None:
                        active_instances.add(row.target_instance_id)
                        continue
                    row.attempt += 1
                    row.status = "running"
                    row.lease_expires_at = now + timedelta(seconds=COMMAND_LEASE_SECONDS)
                    row.updated_at = now
                    active_instances.add(row.target_instance_id)
                    claimed.append(
                        HubCommandV1(
                            command_id=row.id,
                            target_instance_id=row.target_instance_id,
                            runtime_type=row.runtime_type,
                            operation=row.operation,
                            desired_revision=row.desired_revision,
                            attempt=row.attempt,
                            trace_id=row.trace_id,
                            issued_at=_utc(row.issued_at),
                            expires_at=_utc(row.expires_at),
                            create_payload=(
                                row.payload_json
                                if row.operation == HubCommandOperation.CREATE_INSTANCE.value
                                else None
                            ),
                            operation_payload=(
                                row.payload_json
                                if row.operation in {
                                    HubCommandOperation.UPDATE_INSTANCE_MODEL.value,
                                    HubCommandOperation.GET_INSTANCE_LOGS.value,
                                    HubCommandOperation.DIAGNOSE_INSTANCE.value,
                                }
                                else None
                            ),
                        )
                    )
                session.query(MachineHubCommand).filter(
                    MachineHubCommand.hub_id == hub_id,
                    MachineHubCommand.status.in_(["pending", "running"]),
                    MachineHubCommand.expires_at <= now,
                ).update(
                    {MachineHubCommand.status: "expired", MachineHubCommand.lease_expires_at: None},
                    synchronize_session=False,
                )
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command queue is unavailable") from exc
        return HubCommandPollResponseV1(protocol_version="1.0", server_time=now, commands=claimed)

    def renew_command_lease(
        self,
        hub_id: str,
        command_id: str,
        attempt: int,
    ) -> HubCommandLeaseV1:
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                command = session.get(MachineHubCommand, command_id, with_for_update=True)
                if command is None or command.hub_id != hub_id:
                    raise HubNotFoundError("Hub command not found")
                lease_expires_at = _utc(command.lease_expires_at) if command.lease_expires_at is not None else None
                if (
                    command.attempt != attempt
                    or command.status not in {"running", "superseded"}
                    or lease_expires_at is None
                    or lease_expires_at <= now
                    or _utc(command.expires_at) <= now
                ):
                    raise HubConflictError("Hub command lease is no longer active for this attempt")
                command.lease_expires_at = now + timedelta(seconds=COMMAND_LEASE_SECONDS)
                command.updated_at = now
                renewed_until = command.lease_expires_at
        except HubServiceError:
            raise
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub command storage is unavailable") from exc
        return HubCommandLeaseV1(
            protocol_version="1.0",
            command_id=command_id,
            attempt=attempt,
            server_time=now,
            lease_expires_at=renewed_until,
        )

    def accept_events(self, hub_id: str, batch: HubEventBatchRequestV1) -> HubEventBatchAckV1:
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                hub = session.get(MachineHub, hub_id, with_for_update=True)
                if hub is None:
                    raise HubNotFoundError("Hub not found")
                cursor = session.scalar(
                    select(func.max(MachineHubEvent.sequence)).where(MachineHubEvent.hub_id == hub_id)
                ) or 0
                if batch.after_sequence > cursor:
                    raise HubConflictError("event cursor is ahead of the server")
                for event in batch.events:
                    payload = event.model_dump(mode="json")
                    existing = session.scalar(
                        select(MachineHubEvent).where(
                            MachineHubEvent.hub_id == hub_id,
                            MachineHubEvent.sequence == event.sequence,
                        )
                    )
                    if existing is not None:
                        if existing.event_id != str(event.event_id) or existing.payload != payload:
                            raise HubConflictError("event sequence was reused with different content")
                        continue
                    if event.sequence != cursor + 1:
                        raise HubConflictError("event sequence has a gap")
                    duplicate_event_id = session.scalar(
                        select(MachineHubEvent.id).where(
                            MachineHubEvent.hub_id == hub_id,
                            MachineHubEvent.event_id == str(event.event_id),
                        )
                    )
                    if duplicate_event_id is not None:
                        raise HubConflictError("event id was reused at another sequence")
                    session.add(
                        MachineHubEvent(
                            id=str(uuid4()),
                            hub_id=hub_id,
                            event_id=str(event.event_id),
                            sequence=event.sequence,
                            event_type=event.type,
                            payload=payload,
                            occurred_at=_utc(event.occurred_at),
                            received_at=now,
                        )
                    )
                    cursor = event.sequence
                    if isinstance(event, HubCommandEventV1):
                        command = session.get(MachineHubCommand, str(event.command_id), with_for_update=True)
                        if command is None or command.hub_id != hub_id:
                            raise HubConflictError("event references an unknown command")
                        if event.desired_revision != command.desired_revision:
                            raise HubConflictError("event desired revision does not match its command")
                        expected_result = expected_result_kind(HubCommandOperation(command.operation))
                        actual_result = event.result.kind if event.result is not None else None
                        if actual_result is not None and actual_result != expected_result:
                            raise HubConflictError("event result does not match its command operation")
                        if event.phase.value == "succeeded" and expected_result and actual_result != expected_result:
                            raise HubConflictError("successful command event is missing its typed result")
                        result_instance_id = (
                            getattr(event.result, "instance_id", None) or getattr(event.result, "id", None)
                            if event.result is not None
                            else None
                        )
                        if result_instance_id and result_instance_id != command.target_instance_id:
                            raise HubConflictError("event result references a different Runtime Instance")
                        if _utc(command.expires_at) <= now and command.status not in TERMINAL_COMMAND_PHASES:
                            command.status = "expired"
                            command.lease_expires_at = None
                            command.updated_at = now
                        elif event.attempt == command.attempt and command.status == "superseded":
                            if event.phase.value in TERMINAL_COMMAND_PHASES:
                                command.lease_expires_at = None
                                command.updated_at = now
                        elif (
                            event.attempt == command.attempt
                            and command.status not in TERMINAL_COMMAND_PHASES
                            and command.status != "superseded"
                            and command.lease_expires_at is not None
                            and _utc(command.lease_expires_at) > now
                        ):
                            command.status = "running" if event.phase.value in {"accepted", "running"} else event.phase.value
                            command.result_json = payload
                            command.updated_at = now
                            if event.phase.value in TERMINAL_COMMAND_PHASES:
                                command.lease_expires_at = None
                                if command.runtime_type == "openclaw":
                                    catalog_instance = session.get(
                                        AgentInstance,
                                        command.target_instance_id,
                                        with_for_update=True,
                                    )
                                    if catalog_instance is not None:
                                        observed_state = event.observed_state.value if event.observed_state else None
                                        if observed_state:
                                            catalog_instance.observed_state = observed_state
                                        elif event.phase.value == "failed":
                                            catalog_instance.observed_state = "error"
                                        elif event.phase.value == "unknown":
                                            catalog_instance.observed_state = "unknown"
                                        if event.phase.value == "succeeded":
                                            catalog_instance.last_error = None
                                        elif event.error_code:
                                            catalog_instance.last_error = event.error_code
                                        if event.phase.value == "succeeded" and event.result is not None:
                                            if event.result.kind == "model_updated":
                                                config = dict(catalog_instance.config_json or {})
                                                config["model"] = event.result.model
                                                catalog_instance.config_json = config
                                            elif event.result.kind == "instance_deleted":
                                                session.execute(
                                                    delete(InstanceConfig).where(
                                                        InstanceConfig.instance_id == catalog_instance.id
                                                    )
                                                )
                                                session.delete(catalog_instance)
                                                catalog_instance = None
                                        if catalog_instance is not None:
                                            catalog_instance.updated_at = now
                            else:
                                command.lease_expires_at = now + timedelta(seconds=COMMAND_LEASE_SECONDS)
                hub.status = "online"
                hub.last_seen_at = now
                hub.updated_at = now
        except HubServiceError:
            raise
        except IntegrityError as exc:
            raise HubConflictError("Hub event sequence was concurrently accepted; retry from the acknowledged cursor") from exc
        except SQLAlchemyError as exc:
            raise RuntimeError("Hub event storage is unavailable") from exc
        return HubEventBatchAckV1(protocol_version="1.0", accepted_through_sequence=cursor, accepted_at=now)
