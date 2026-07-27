import base64

from sqlalchemy import text

from server.config import reset_config
from server.db.engine import get_engine, reset_engine


_KEY = base64.b64encode(b"01234567890123456789012345678901").decode()


def test_engine_starts_from_minimal_bootstrap(tmp_path, monkeypatch):
    db_path = tmp_path / "minimal.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    engine = get_engine()

    assert engine.url.database == str(db_path)
    assert engine.echo is False
    reset_engine()


def test_file_sqlite_url_creates_parent_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "app.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    get_engine()

    assert db_path.parent.is_dir()
    reset_engine()


async def test_default_sqlite_engine_enforces_foreign_keys(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "foreign-keys.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", _KEY)
    reset_config()
    reset_engine()

    engine = get_engine()
    async with engine.connect() as connection:
        enabled = await connection.scalar(text("PRAGMA foreign_keys"))

    assert enabled == 1
    await engine.dispose()
    reset_engine()
