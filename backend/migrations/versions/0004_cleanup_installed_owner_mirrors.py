"""Clean up duplicate global mirrors before owner backfill.

Revision ID: 0004_owner_mirrors
Revises: 0003_installed_owner
Create Date: 2026-10-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_owner_mirrors"
down_revision: Union[str, None] = "0003_installed_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM installed_agents AS legacy "
            "WHERE legacy.owner_id = 'project' "
            "AND EXISTS ("
            "  SELECT 1 FROM lifecycle_tasks AS task "
            "  WHERE task.id = legacy.task_id AND task.owner_id <> legacy.owner_id "
            "  AND EXISTS ("
            "    SELECT 1 FROM installed_agents AS scoped "
            "    WHERE scoped.owner_id = task.owner_id AND scoped.app_id = legacy.app_id"
            "  )"
            ")"
        )
    )
    op.execute(
        sa.text(
            "UPDATE installed_agents "
            "SET owner_id = (SELECT owner_id FROM lifecycle_tasks WHERE lifecycle_tasks.id = installed_agents.task_id) "
            "WHERE task_id IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM lifecycle_tasks WHERE lifecycle_tasks.id = installed_agents.task_id)"
        )
    )


def downgrade() -> None:
    # The mirror rows are superseded current-state projections, not audit data.
    pass
