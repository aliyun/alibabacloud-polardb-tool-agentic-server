from __future__ import annotations

import io

import pytest

from server.polarrag.oss import (
    OssOperationError,
    OssObjectStore,
    normalize_prefix,
    validate_endpoint,
    validate_oss_write_access,
)


class FakeBucket:
    def __init__(self, *, fail_put: bool = False, fail_delete: bool = False) -> None:
        self.fail_put = fail_put
        self.fail_delete = fail_delete
        self.puts: list[tuple[str, bytes]] = []
        self.deletes: list[str] = []

    def put_object(self, key: str, content) -> None:
        if self.fail_put:
            raise RuntimeError("sensitive upstream error")
        self.puts.append((key, content.read()))

    def delete_object(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("sensitive upstream error")
        self.deletes.append(key)


def test_oss_endpoint_and_prefix_reject_unsafe_values() -> None:
    assert validate_endpoint(" https://oss.example.test/ ") == "https://oss.example.test"
    assert validate_endpoint(" oss-cn-hangzhou.aliyuncs.com ") == (
        "oss-cn-hangzhou.aliyuncs.com"
    )
    assert normalize_prefix("/pas/documents/") == "pas/documents"
    for value in (
        "ftp://oss.example.test",
        "https://user:secret@oss.example.test",
        "https://oss.example.test/path",
        "https://oss.example.test?token=secret",
    ):
        with pytest.raises(ValueError):
            validate_endpoint(value)
    for value in ("", "/", "../documents", "pas/../documents"):
        with pytest.raises(ValueError):
            normalize_prefix(value)


async def test_oss_validation_puts_and_deletes_a_probe(monkeypatch) -> None:
    bucket = FakeBucket()
    monkeypatch.setattr(
        "server.polarrag.oss.oss2.Auth",
        lambda access_key_id, access_key_secret: (access_key_id, access_key_secret),
    )
    monkeypatch.setattr(
        "server.polarrag.oss.oss2.Bucket",
        lambda auth, endpoint, name: bucket,
    )

    await validate_oss_write_access(
        endpoint="https://oss.example.test",
        bucket="space-documents",
        access_key_id="ak",
        access_key_secret="sk",
        object_prefix="pas/documents",
    )

    assert len(bucket.puts) == 1
    key, body = bucket.puts[0]
    assert key.startswith("pas/documents/.pas-probe/")
    assert body == b""
    assert bucket.deletes == [key]


async def test_oss_errors_are_sanitized(monkeypatch) -> None:
    bucket = FakeBucket(fail_put=True)
    monkeypatch.setattr("server.polarrag.oss.oss2.Auth", lambda *_args: object())
    monkeypatch.setattr("server.polarrag.oss.oss2.Bucket", lambda *_args: bucket)
    store = OssObjectStore(
        endpoint="https://oss.example.test",
        bucket="space-documents",
        access_key_id="ak",
        access_key_secret="sk",
    )

    with pytest.raises(OssOperationError, match="OSS_OPERATION_FAILED") as error:
        await store.put("path/file", io.BytesIO(b"body"))

    assert "sensitive" not in str(error.value)


async def test_oss_validation_requires_delete_permission(monkeypatch) -> None:
    bucket = FakeBucket(fail_delete=True)
    monkeypatch.setattr("server.polarrag.oss.oss2.Auth", lambda *_args: object())
    monkeypatch.setattr("server.polarrag.oss.oss2.Bucket", lambda *_args: bucket)

    with pytest.raises(
        OssOperationError,
        match="OSS_DELETE_VALIDATION_FAILED",
    ):
        await validate_oss_write_access(
            endpoint="https://oss.example.test",
            bucket="space-documents",
            access_key_id="ak",
            access_key_secret="sk",
            object_prefix="pas/documents",
        )
