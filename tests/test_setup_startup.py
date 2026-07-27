from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path

from server.app import create_app
from server.config import reset_config
from server.db.engine import get_engine, reset_engine
from server.db.schema import migrate_database


async def test_fresh_startup_prints_bootstrap_token_only_once(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "setup.db"
    monkeypatch.setenv(
        "PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}"
    )
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(
            b"01234567890123456789012345678901"
        ).decode(),
    )
    reset_config()
    reset_engine()
    await asyncio.to_thread(
        migrate_database,
        f"sqlite+aiosqlite:///{database}",
    )
    engine = get_engine()

    try:
        first_app = create_app()
        async with first_app.router.lifespan_context(first_app):
            assert first_app.state.provisioning_runtime is not None
        first_terminal = capsys.readouterr()
        match = re.search(
            r"Bootstrap token:\s+(\S+)",
            first_terminal.out + first_terminal.err,
        )
        assert match is not None
        assert len(match.group(1)) >= 32

        second_app = create_app()
        async with second_app.router.lifespan_context(second_app):
            pass
        second_terminal = capsys.readouterr()
        assert "Bootstrap token:" not in (
            second_terminal.out + second_terminal.err
        )
    finally:
        await engine.dispose()
        reset_engine()
        reset_config()
