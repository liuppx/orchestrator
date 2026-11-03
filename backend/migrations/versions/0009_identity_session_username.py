"""Retain verified usernames for short-lived identity login sessions.

Revision ID: 0009_identity_session_username
Revises: 0008_hub_runtime_create
Create Date: 2026-11-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_identity_session_username"
down_revision: Union[str, None] = "0008_hub_runtime_create"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("identity_login_sessions", sa.Column("username", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("identity_login_sessions", "username")
