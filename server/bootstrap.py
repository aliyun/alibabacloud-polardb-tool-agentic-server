from __future__ import annotations

import base64
import binascii
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_BOOTSTRAP_ENV = frozenset(
    {"PAS_DATABASE_URL", "PAS_ENCRYPTION_KEY"}
)
_MAX_KEY_FILE_BYTES = 4096


class BootstrapConfigError(ValueError):
    """Raised when the minimal process bootstrap configuration is invalid."""


@dataclass(frozen=True, slots=True)
class BootstrapSettings:
    database_url: str
    encryption_key: bytes


def read_restricted_secret_file(
    raw_path: str,
    *,
    setting_name: str,
    max_bytes: int = _MAX_KEY_FILE_BYTES,
) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        raise BootstrapConfigError(
            f"{setting_name} must use an absolute path"
        )
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise BootstrapConfigError(
            f"{setting_name} is not readable"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise BootstrapConfigError(
            f"{setting_name} must resolve to a regular file"
        )
    if metadata.st_size > max_bytes:
        raise BootstrapConfigError(f"{setting_name} is too large")
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BootstrapConfigError(
            f"{setting_name} permissions must not allow group or other access"
        )
    try:
        return resolved.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise BootstrapConfigError(
            f"{setting_name} is not readable"
        ) from error


def _read_key_source(value: str) -> str:
    if not value.startswith("file:"):
        return value.strip()
    return read_restricted_secret_file(
        value.removeprefix("file:"),
        setting_name="PAS_ENCRYPTION_KEY file reference",
    )


def load_bootstrap_settings(
    env: Mapping[str, str] | None = None,
) -> BootstrapSettings:
    source = os.environ if env is None else env
    database_url = source.get("PAS_DATABASE_URL", "").strip()
    if not database_url:
        raise BootstrapConfigError("PAS_DATABASE_URL is required")

    return BootstrapSettings(
        database_url=database_url,
        encryption_key=load_root_encryption_key(source),
    )


def load_root_encryption_key(
    env: Mapping[str, str] | None = None,
) -> bytes:
    """Validate the root key independently for encryption consumers."""
    source = os.environ if env is None else env
    encoded_key = _read_key_source(
        source.get("PAS_ENCRYPTION_KEY", "").strip()
    )
    if not encoded_key:
        raise BootstrapConfigError("PAS_ENCRYPTION_KEY is required")
    try:
        encryption_key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise BootstrapConfigError(
            "PAS_ENCRYPTION_KEY must be valid base64"
        ) from error
    if len(encryption_key) != 32:
        raise BootstrapConfigError(
            "PAS_ENCRYPTION_KEY must decode to exactly 32 bytes"
        )
    return encryption_key
