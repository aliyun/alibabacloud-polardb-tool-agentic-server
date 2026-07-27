from __future__ import annotations

import base64
from pathlib import Path

import pytest

from server.bootstrap import (
    SUPPORTED_BOOTSTRAP_ENV,
    BootstrapConfigError,
    load_bootstrap_settings,
)


KEY_BYTES = b"01234567890123456789012345678901"
KEY_B64 = base64.b64encode(KEY_BYTES).decode()


def test_loads_exact_bootstrap_contract() -> None:
    settings = load_bootstrap_settings(
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": KEY_B64,
        }
    )

    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.encryption_key == KEY_BYTES
    assert SUPPORTED_BOOTSTRAP_ENV == frozenset(
        {"PAS_DATABASE_URL", "PAS_ENCRYPTION_KEY"}
    )


def test_reads_key_from_absolute_file(tmp_path: Path) -> None:
    key_file = tmp_path / "root.key"
    key_file.write_text(KEY_B64)
    key_file.chmod(0o600)

    settings = load_bootstrap_settings(
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": f"file:{key_file}",
        }
    )

    assert settings.encryption_key == KEY_BYTES


def test_allows_projected_secret_symlink(tmp_path: Path) -> None:
    version_dir = tmp_path / "..2026_07_26"
    version_dir.mkdir()
    target = version_dir / "root.key"
    target.write_text(KEY_B64)
    target.chmod(0o600)
    link = tmp_path / "root.key"
    link.symlink_to(target)

    settings = load_bootstrap_settings(
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": f"file:{link}",
        }
    )

    assert settings.encryption_key == KEY_BYTES


@pytest.mark.parametrize(
    "env",
    [
        {"PAS_ENCRYPTION_KEY": KEY_B64},
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": "",
        },
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": "not-base64",
        },
        {
            "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "PAS_ENCRYPTION_KEY": base64.b64encode(b"short").decode(),
        },
    ],
)
def test_rejects_invalid_bootstrap_values(env: dict[str, str]) -> None:
    with pytest.raises(BootstrapConfigError):
        load_bootstrap_settings(env)


def test_rejects_relative_key_file() -> None:
    with pytest.raises(BootstrapConfigError, match="absolute"):
        load_bootstrap_settings(
            {
                "PAS_DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                "PAS_ENCRYPTION_KEY": "file:relative/root.key",
            }
        )

