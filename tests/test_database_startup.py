from __future__ import annotations

import base64

import pytest
from unittest.mock import AsyncMock

from server.app import create_app
from server.config import reset_config
from server.db.engine import reset_engine
from server.db.schema import DatabaseSchemaError


@pytest.mark.parametrize(
    "code",
    [
        "DATABASE_SCHEMA_NOT_INITIALIZED",
        "DATABASE_SCHEMA_OUTDATED",
        "DATABASE_SCHEMA_TOO_NEW",
        "DATABASE_MIGRATION_HEAD_INVALID",
        "DATABASE_UNAVAILABLE",
    ],
)
async def test_lifespan_stops_before_configuration_initialization(
    code: str,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(
        "PAS_DATABASE_URL",
        f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}",
    )
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(b"0" * 32).decode(),
    )

    async def reject_schema() -> str:
        raise DatabaseSchemaError(code, "sanitized")

    initialize = AsyncMock(
        side_effect=AssertionError(
            "configuration initialization must not run"
        )
    )
    monkeypatch.setattr(
        "server.app.check_database_schema",
        reject_schema,
        raising=False,
    )
    monkeypatch.setattr(
        "server.configuration.bootstrap.initialize_configuration",
        initialize,
    )
    monkeypatch.setattr(
        "server.app.setup_logging",
        lambda *_args, **_kwargs: None,
    )
    reset_config()
    reset_engine()
    app = create_app()

    try:
        with pytest.raises(DatabaseSchemaError) as captured:
            async with app.router.lifespan_context(app):
                pass
        assert captured.value.code == code
        initialize.assert_not_awaited()
        assert not getattr(app.state, "background_tasks", set())
    finally:
        reset_engine()
        reset_config()
