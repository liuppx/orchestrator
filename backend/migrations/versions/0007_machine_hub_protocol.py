"""Persist machine Hub identities, commands, inventory and events.

Revision ID: 0007_machine_hub_protocol
Revises: 0006_identity_login_sessions
Create Date: 2026-10-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql


revision: str = "0007_machine_hub_protocol"
down_revision: Union[str, None] = "0006_identity_login_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_VALUE = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "machine_hubs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("protocol_version", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", JSON_VALUE, nullable=False),
        sa.Column("inventory_revision", sa.Integer(), nullable=False),
        sa.Column("inventory_json", JSON_VALUE, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "machine_hub_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("hub_id", sa.String(length=128), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["hub_id"], ["machine_hubs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_machine_hub_credentials_hub_status",
        "machine_hub_credentials",
        ["hub_id", "status"],
    )
    op.create_table(
        "machine_hub_commands",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("hub_id", sa.String(length=128), nullable=False),
        sa.Column("target_instance_id", sa.String(length=128), nullable=False),
        sa.Column("runtime_type", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("desired_revision", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", JSON_VALUE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["hub_id"], ["machine_hubs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hub_id", "idempotency_key", name="uq_machine_hub_commands_hub_idempotency"),
    )
    op.create_index(
        "ix_machine_hub_commands_hub_status_expiry",
        "machine_hub_commands",
        ["hub_id", "status", "expires_at"],
    )
    op.create_table(
        "machine_hub_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("hub_id", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", JSON_VALUE, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["hub_id"], ["machine_hubs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hub_id", "sequence", name="uq_machine_hub_events_hub_sequence"),
        sa.UniqueConstraint("hub_id", "event_id", name="uq_machine_hub_events_hub_event_id"),
    )

def downgrade() -> None:
    op.drop_table("machine_hub_events")
    op.drop_index("ix_machine_hub_commands_hub_status_expiry", table_name="machine_hub_commands")
    op.drop_table("machine_hub_commands")
    op.drop_index("ix_machine_hub_credentials_hub_status", table_name="machine_hub_credentials")
    op.drop_table("machine_hub_credentials")
    op.drop_table("machine_hubs")
