"""Add instance deployment mode, schedule definition and scheduled run records.

Revision ID: 0010_instance_deployment_mode
Revises: 0009_identity_session_username
Create Date: 2026-11-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010_instance_deployment_mode"
down_revision: Union[str, None] = "0009_identity_session_username"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_instances") as batch_op:
        batch_op.add_column(
            sa.Column("deployment_mode", sa.String(length=16), nullable=False, server_default="service")
        )
        batch_op.add_column(sa.Column("schedule_json", sa.JSON(), nullable=True))

    op.create_table(
        "scheduled_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(length=255),
            sa.ForeignKey("agent_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="schedule"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("log_ref", sa.String(length=1024), nullable=True),
        sa.Column("command_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "scheduled_for", name="uq_scheduled_runs_instance_slot"),
    )
    op.create_index("ix_scheduled_runs_instance_id", "scheduled_runs", ["instance_id"])
    op.create_index("ix_scheduled_runs_owner_id", "scheduled_runs", ["owner_id"])
    op.create_index("ix_scheduled_runs_instance_state", "scheduled_runs", ["instance_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_runs_instance_state", table_name="scheduled_runs")
    op.drop_index("ix_scheduled_runs_owner_id", table_name="scheduled_runs")
    op.drop_index("ix_scheduled_runs_instance_id", table_name="scheduled_runs")
    op.drop_table("scheduled_runs")
    with op.batch_alter_table("agent_instances") as batch_op:
        batch_op.drop_column("schedule_json")
        batch_op.drop_column("deployment_mode")
