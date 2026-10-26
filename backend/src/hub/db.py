from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def init_database(database_url: str) -> None:
    """Apply the versioned schema for local development and explicit setup."""
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
