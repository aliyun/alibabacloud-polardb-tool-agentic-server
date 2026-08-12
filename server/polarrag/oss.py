from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import BinaryIO, cast
from urllib.parse import urlsplit

import oss2  # type: ignore[import-untyped]


class OssOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class OssMultipartPart:
    part_number: int
    etag: str
    size: int


def validate_endpoint(value: str) -> str:
    candidate = value.strip().rstrip("/")
    has_scheme = "://" in candidate
    parsed = urlsplit(candidate if has_scheme else f"//{candidate}")
    if (
        (has_scheme and parsed.scheme not in {"http", "https"})
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid OSS endpoint")
    return candidate


def normalize_prefix(value: str) -> str:
    parts = [part for part in value.strip().strip("/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("invalid OSS object prefix")
    return "/".join(parts)


class OssObjectStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        self._bucket = oss2.Bucket(
            oss2.Auth(access_key_id, access_key_secret),
            validate_endpoint(endpoint),
            bucket,
        )

    async def put(self, key: str, content: BinaryIO | bytes) -> None:
        try:
            await asyncio.to_thread(self._bucket.put_object, key, content)
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._bucket.delete_object, key)
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    async def initiate_multipart(self, key: str) -> str:
        try:
            result = await asyncio.to_thread(
                self._bucket.init_multipart_upload,
                key,
            )
            upload_id = result.upload_id
            if not isinstance(upload_id, str) or not upload_id:
                raise ValueError("invalid OSS multipart upload ID")
            return upload_id
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    def sign_part_url(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        *,
        expires_seconds: int,
    ) -> str:
        try:
            return cast(
                str,
                self._bucket.sign_url(
                    "PUT",
                    key,
                    expires_seconds,
                    params={
                        "uploadId": upload_id,
                        "partNumber": str(part_number),
                    },
                ),
            )
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    async def list_multipart_parts(
        self,
        key: str,
        upload_id: str,
    ) -> list[OssMultipartPart]:
        try:
            result = await asyncio.to_thread(
                self._bucket.list_parts,
                key,
                upload_id,
                max_parts=1000,
            )
            if result.is_truncated:
                raise ValueError("too many OSS multipart parts")
            return [
                OssMultipartPart(
                    part_number=part.part_number,
                    etag=part.etag,
                    size=part.size,
                )
                for part in result.parts
            ]
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    async def complete_multipart(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[OssMultipartPart],
    ) -> None:
        try:
            part_infos = [
                oss2.models.PartInfo(part.part_number, part.etag)
                for part in parts
            ]
            await asyncio.to_thread(
                self._bucket.complete_multipart_upload,
                key,
                upload_id,
                part_infos,
            )
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._bucket.abort_multipart_upload,
                key,
                upload_id,
            )
        except Exception as exc:
            raise OssOperationError("OSS_OPERATION_FAILED") from exc


async def validate_oss_write_access(
    *,
    endpoint: str,
    bucket: str,
    access_key_id: str,
    access_key_secret: str,
    object_prefix: str,
) -> None:
    store = OssObjectStore(
        endpoint=endpoint,
        bucket=bucket,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
    )
    key = f"{normalize_prefix(object_prefix)}/.pas-probe/{uuid.uuid4().hex}"
    await store.put(key, io.BytesIO(b""))
    try:
        await store.delete(key)
    except OssOperationError:
        raise OssOperationError("OSS_DELETE_VALIDATION_FAILED") from None
