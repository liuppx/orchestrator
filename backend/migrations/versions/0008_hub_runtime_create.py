"""Add typed Hub runtime create payloads and unallocated catalog ports.

Revision ID: 0008_hub_runtime_create
Revises: 0007_machine_hub_protocol
Create Date: 2026-11-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_hub_runtime_create"
down_revision: Union[str, None] = "0007_machine_hub_protocol"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("machine_hub_commands", sa.Column("payload_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("agent_instances") as batch_op:
        batch_op.alter_column("port", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("agent_instances") as batch_op:
        batch_op.alter_column("port", existing_type=sa.Integer(), nullable=False)
    op.drop_column("machine_hub_commands", "payload_json")
