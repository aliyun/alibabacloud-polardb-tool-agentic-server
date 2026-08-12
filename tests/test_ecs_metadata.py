from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from server.aliyun.ecs_metadata import (
    ECSMetadataError,
    resolve_ecs_role_name_v2,
)


@dataclass
class _Response:
    text: str

    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def put(self, path: str, *, headers: dict[str, str]) -> _Response:
        self.requests.append(("PUT", path, headers))
        return _Response("imds-v2-token")

    async def get(self, path: str, *, headers: dict[str, str]) -> _Response:
        self.requests.append(("GET", path, headers))
        return _Response("pas-runtime\n")


async def test_resolve_ecs_role_name_uses_fixed_host_imdsv2_without_proxy() -> None:
    client = _Client()
    created: dict[str, object] = {}

    def factory(**kwargs):
        created.update(kwargs)
        return client

    role = await resolve_ecs_role_name_v2(factory)

    assert role == "pas-runtime"
    assert created["base_url"] == "http://100.100.100.200"
    assert created["trust_env"] is False
    assert client.requests == [
        (
            "PUT",
            "/latest/api/token",
            {"X-aliyun-ecs-metadata-token-ttl-seconds": "21600"},
        ),
        (
            "GET",
            "/latest/meta-data/ram/security-credentials/",
            {"X-aliyun-ecs-metadata-token": "imds-v2-token"},
        ),
    ]


async def test_resolve_ecs_role_name_does_not_fallback_when_token_fails() -> None:
    class TokenFailureClient(_Client):
        async def put(self, path: str, *, headers: dict[str, str]) -> _Response:
            raise RuntimeError("token unavailable with sensitive body")

        async def get(self, path: str, *, headers: dict[str, str]) -> _Response:
            raise AssertionError("must not attempt IMDSv1 fallback")

    with pytest.raises(ECSMetadataError) as error:
        await resolve_ecs_role_name_v2(lambda **_: TokenFailureClient())
    assert error.value.reason == "imdsv2_unavailable"
    assert "sensitive body" not in str(error.value)


async def test_token_endpoint_404_is_not_misreported_as_missing_role() -> None:
    class TokenNotFoundClient(_Client):
        async def put(self, path: str, *, headers: dict[str, str]) -> _Response:
            request = httpx.Request("PUT", "http://100.100.100.200" + path)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError(
                "secret response body", request=request, response=response
            )

    with pytest.raises(ECSMetadataError) as error:
        await resolve_ecs_role_name_v2(lambda **_: TokenNotFoundClient())

    assert error.value.reason == "imdsv2_unavailable"
