"""Shared pytest fixtures and test helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of how pytest is invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


def make_settings(**overrides) -> Settings:
    """Build a Settings instance isolated from any local .env file."""
    overrides.setdefault("_env_file", None)
    return Settings(**overrides)


@pytest.fixture
def config(tmp_path):
    """A valid configuration for tests (isolated DB, dummy credentials)."""
    return make_settings(
        database_path=str(tmp_path / "test.sqlite"),
        telegram_bot_token="123456789:AAfaketokenvaluefortests_only",
        telegram_chat_id="-1001234567890",
    )


@pytest.fixture
async def db(config):
    """A connected, migrated database on a temporary path."""
    from app.persistence.database import Database
    from app.persistence.migrations import apply_migrations

    database = Database(config.database_path)
    await database.connect()
    await apply_migrations(database)
    yield database
    await database.close()
