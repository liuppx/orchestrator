"""Persist short-lived wallet identity and passkey login sessions.

Revision ID: 0006_identity_login_sessions
Revises: 0005_task_retry_schedule
Create Date: 2026-10-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_identity_login_sessions"
down_revision: Union[str, None] = "0005_task_retry_schedule"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "identity_login_sessions",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=True, unique=True),
        sa.Column("state", sa.String(length=128), nullable=True, unique=True),
        sa.Column("redirect_uri", sa.String(length=1024), nullable=True),
        sa.Column("code_verifier", sa.String(length=255), nullable=True),
        sa.Column("audience", sa.String(length=1024), nullable=True),
        sa.Column("nonce", sa.String(length=255), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authorization_code", sa.String(length=2048), nullable=True),
        sa.Column("did", sa.String(length=255), nullable=True),
        sa.Column("wallet_address", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_identity_login_sessions_status_expires_at", "identity_login_sessions", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_identity_login_sessions_status_expires_at", table_name="identity_login_sessions")
    op.drop_table("identity_login_sessions")
