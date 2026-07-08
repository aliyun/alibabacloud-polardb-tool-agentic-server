from server.config import reset_config
from server.db.engine import get_engine, reset_engine


def test_file_sqlite_url_creates_parent_directory(tmp_path, monkeypatch):
    db_path = tmp_path / "nested" / "app.db"
    monkeypatch.setenv("PAS_SERVER_DEV_MODE", "true")
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    reset_config()
    reset_engine()

    get_engine()

    assert db_path.parent.is_dir()
    reset_engine()
