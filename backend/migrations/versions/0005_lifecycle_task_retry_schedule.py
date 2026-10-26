"""Add persistent retry scheduling for lifecycle tasks.

Revision ID: 0005_task_retry_schedule
Revises: 0004_owner_mirrors
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_task_retry_schedule"
down_revision: Union[str, None] = "0004_owner_mirrors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("lifecycle_tasks") as batch_op:
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_lifecycle_tasks_next_attempt_at", ["status", "next_attempt_at", "created_at"])


def downgrade() -> None:
    with op.batch_alter_table("lifecycle_tasks") as batch_op:
        batch_op.drop_index("ix_lifecycle_tasks_next_attempt_at")
        batch_op.drop_column("next_attempt_at")
