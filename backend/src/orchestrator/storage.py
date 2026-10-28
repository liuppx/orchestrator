from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class RuntimeNode(Base):
    __tablename__ = "runtime_nodes"

    id = mapped_column(String(128), primary_key=True)
    node_key = mapped_column(String(255), nullable=False, unique=True)
    status = mapped_column(String(32), nullable=False, default="unknown")
    last_seen_at = mapped_column(DateTime(timezone=True))
    capacity = mapped_column(JSON_VALUE, nullable=False, default=dict)
    version = mapped_column(Integer, nullable=False, default=1)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)


class AgentInstance(Base):
    __tablename__ = "agent_instances"
    __table_args__ = (UniqueConstraint("node_id", "port", name="uq_agent_instances_node_port"),)

    id = mapped_column(String(255), primary_key=True)
    owner_id = mapped_column(String(255), nullable=False, index=True)
    node_id = mapped_column(String(128), ForeignKey("runtime_nodes.id"), nullable=False)
    runtime_type = mapped_column(String(64), nullable=False)
    profile_ref = mapped_column(String(1024), nullable=False)
    workspace_ref = mapped_column(String(1024), nullable=False)
    kind = mapped_column(String(64), nullable=False, default="agent")
    name = mapped_column(String(255), nullable=False, default="")
    port = mapped_column(Integer, nullable=False)
    desired_state = mapped_column(String(32), nullable=False, default="stopped")
    observed_state = mapped_column(String(32), nullable=False, default="unknown")
    revision = mapped_column(Integer, nullable=False, default=1)
    config_json = mapped_column(JSON_VALUE, nullable=False, default=dict)
    pid = mapped_column(Integer)
    last_error = mapped_column(Text)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)


class InstanceConfig(Base):
    __tablename__ = "instance_configs"

    instance_id = mapped_column(String(255), ForeignKey("agent_instances.id", ondelete="CASCADE"), primary_key=True)
    revision = mapped_column(Integer, primary_key=True)
    config_json = mapped_column(JSON_VALUE, nullable=False, default=dict)
    secret_refs = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class LifecycleTask(Base):
    __tablename__ = "lifecycle_tasks"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_lifecycle_tasks_owner_idempotency"),
        Index("ix_lifecycle_tasks_status_created", "status", "created_at"),
    )

    id = mapped_column(String(255), primary_key=True)
    owner_id = mapped_column(String(255), nullable=False, default="system")
    instance_id = mapped_column(String(255), nullable=False, index=True)
    action = mapped_column(String(64), nullable=False)
    status = mapped_column(String(32), nullable=False)
    app_id = mapped_column(String(255), nullable=False)
    agent_id = mapped_column(String(255), nullable=False)
    version = mapped_column(String(255), nullable=False)
    idempotency_key = mapped_column(String(255))
    payload = mapped_column(JSON_VALUE, nullable=False, default=dict)
    attempts = mapped_column(Integer, nullable=False, default=0)
    lease_owner = mapped_column(String(255))
    lease_expires_at = mapped_column(DateTime(timezone=True))
    next_attempt_at = mapped_column(DateTime(timezone=True))
    result_code = mapped_column(String(64))
    created_at = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)


class InstalledAgent(Base):
    __tablename__ = "installed_agents"

    owner_id = mapped_column(String(255), primary_key=True, default="project")
    app_id = mapped_column(String(255), primary_key=True)
    agent_id = mapped_column(String(255), nullable=False)
    version = mapped_column(String(255), nullable=False)
    status = mapped_column(String(32), nullable=False)
    runtime = mapped_column(String(64), nullable=False)
    task_id = mapped_column(String(255))
    updated_at = mapped_column(DateTime(timezone=True), nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id = mapped_column(String(255), primary_key=True)
    task_id = mapped_column(String(255), ForeignKey("lifecycle_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = mapped_column(String(128), nullable=False)
    metadata_json = mapped_column(JSON_VALUE, nullable=False, default=dict)
    request_id = mapped_column(String(255))
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class HookEvent(Base):
    __tablename__ = "hook_events"

    id = mapped_column(String(255), primary_key=True)
    action = mapped_column(String(128), nullable=False)
    instance_id = mapped_column(String(255), nullable=False, index=True)
    payload = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


class ConsumedChallenge(Base):
    __tablename__ = "consumed_challenges"

    digest = mapped_column(String(64), primary_key=True)
    expires_at = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = mapped_column(String(255), primary_key=True)
    actor = mapped_column(String(255), nullable=False)
    action = mapped_column(String(128), nullable=False)
    resource = mapped_column(String(512), nullable=False)
    outcome = mapped_column(String(64), nullable=False)
    request_id = mapped_column(String(255))
    metadata_json = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_status_retry", "status", "next_attempt_at"),)

    id = mapped_column(String(255), primary_key=True)
    event_type = mapped_column(String(128), nullable=False)
    object_path = mapped_column(String(1024))
    checksum = mapped_column(String(128))
    remote_etag = mapped_column(String(255))
    payload = mapped_column(JSON_VALUE, nullable=False, default=dict)
    status = mapped_column(String(32), nullable=False, default="pending")
    attempts = mapped_column(Integer, nullable=False, default=0)
    lease_owner = mapped_column(String(255))
    lease_expires_at = mapped_column(DateTime(timezone=True))
    last_error = mapped_column(Text)
    next_attempt_at = mapped_column(DateTime(timezone=True))
    delivered_at = mapped_column(DateTime(timezone=True))
    created_at = mapped_column(DateTime(timezone=True), nullable=False)


@lru_cache(maxsize=8)
def engine_for(database_url: str) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite:"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **options)
    if database_url.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


@lru_cache(maxsize=8)
def sessions_for(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=engine_for(database_url), expire_on_commit=False)
