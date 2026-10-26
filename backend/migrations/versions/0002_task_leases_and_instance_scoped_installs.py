"""Add task/outbox lease fields and scope installed apps by owner.

Revision ID: 0002_task_leases
Revises: 0001_initial
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_task_leases"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Rebuild this small table so the migration works on both PostgreSQL and
    # SQLite, whose implicit primary-key constraint names differ.
    op.create_table(
        "installed_agents_new",
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("app_id", sa.String(length=255), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("version", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("runtime", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner_id", "app_id", name="pk_installed_agents"),
    )
    op.execute(
        sa.text(
            "INSERT INTO installed_agents_new "
            "(owner_id, app_id, agent_id, version, status, runtime, task_id, updated_at) "
            "SELECT 'project', app_id, agent_id, version, status, runtime, task_id, updated_at "
            "FROM installed_agents"
        )
    )
    op.drop_table("installed_agents")
    op.rename_table("installed_agents_new", "installed_agents")

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("remote_etag", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_column("remote_etag")
        batch_op.drop_column("last_error")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")

    with op.batch_alter_table("installed_agents") as batch_op:
        batch_op.drop_constraint("pk_installed_agents", type_="primary")
        batch_op.create_primary_key(None, ["app_id"])
        batch_op.drop_column("owner_id")
