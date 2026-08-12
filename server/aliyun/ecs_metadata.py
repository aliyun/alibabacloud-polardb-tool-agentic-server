from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx


class ECSMetadataError(RuntimeError):
    """A sanitized failure while obtaining an ECS RAM role through IMDSv2."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _metadata_status_error(
    error: httpx.HTTPStatusError, *, role_request: bool
) -> ECSMetadataError:
    status = error.response.status_code
    if status in {401, 403}:
        return ECSMetadataError("metadata_disabled")
    if role_request and status == 404:
        return ECSMetadataError("role_not_attached")
    return ECSMetadataError("imdsv2_unavailable")


async def resolve_ecs_role_name_v2(
    client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> str:
    """Return the attached RAM role through the fixed IMDSv2 endpoint only."""
    try:
        async with client_factory(
            base_url="http://100.100.100.200",
            trust_env=False,
            timeout=httpx.Timeout(1.0),
        ) as client:
            token = await client.put(
                "/latest/api/token",
                headers={
                    "X-aliyun-ecs-metadata-token-ttl-seconds": "21600"
                },
            )
            try:
                token.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise _metadata_status_error(
                    error, role_request=False
                ) from None
            role = await client.get(
                "/latest/meta-data/ram/security-credentials/",
                headers={"X-aliyun-ecs-metadata-token": token.text},
            )
            try:
                role.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise _metadata_status_error(
                    error, role_request=True
                ) from None
    except ECSMetadataError:
        raise
    except Exception:
        raise ECSMetadataError("imdsv2_unavailable") from None

    role_name = role.text.strip()
    if not role_name:
        raise ECSMetadataError("role_not_attached")
    return role_name
