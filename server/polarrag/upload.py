from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import PurePath
from typing import Any, BinaryIO

from server.core.crypto import decrypt
from server.models import PolarRAGSpace
from server.polarrag.oss import OssObjectStore

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def validate_filename(value: str | None) -> str:
    if not value or len(value) > 512 or _CONTROL_CHARACTER.search(value):
        raise ValueError("invalid filename")
    if "\\" in value or PurePath(value).name != value or value in {".", ".."}:
        raise ValueError("invalid filename")
    return value


def file_type_from_filename(filename: str) -> str:
    if "." not in filename:
        raise ValueError("file extension required")
    file_type = filename.rsplit(".", 1)[-1].lower()
    if not file_type or len(file_type) > 32 or not file_type.isalnum():
        raise ValueError("invalid file extension")
    return file_type


def checksum_file(content: BinaryIO) -> tuple[str, str, int]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
    content.seek(0)
    while chunk := content.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise ValueError("file too large")
        md5.update(chunk)
        sha256.update(chunk)
    content.seek(0)
    if size == 0:
        raise ValueError("empty file")
    return md5.hexdigest(), sha256.hexdigest(), size


def object_store_from_space(space: PolarRAGSpace) -> OssObjectStore:
    if (
        not space.oss_bucket
        or not space.oss_endpoint
        or not space.oss_access_key_id_ciphertext
        or not space.oss_access_key_secret_ciphertext
    ):
        raise ValueError("OSS configuration unavailable")
    return OssObjectStore(
        endpoint=space.oss_endpoint,
        bucket=space.oss_bucket,
        access_key_id=decrypt(space.oss_access_key_id_ciphertext),
        access_key_secret=decrypt(space.oss_access_key_secret_ciphertext),
    )


def document_object_key(prefix: str, upload_object_id: str, filename: str) -> str:
    return f"{prefix}/{upload_object_id}/{filename}"


def document_actor(acl_context: dict[str, Any]) -> dict[str, str]:
    for principal in acl_context.get("principals", []):
        if not isinstance(principal, dict) or principal.get("type") != "user":
            continue
        provider = principal.get("provider")
        principal_id = principal.get("id")
        if (
            isinstance(provider, str)
            and provider
            and isinstance(principal_id, str)
            and principal_id
        ):
            return {"provider": provider, "type": "user", "id": principal_id}
    raise ValueError("user principal unavailable")


def new_upload_object_id() -> str:
    return uuid.uuid4().hex
