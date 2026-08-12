from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import Future
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from server.aliyun.credential_observability import (
    CredentialMetricSample,
    emit_credential_metric,
    set_credential_metric_sink,
)
from server.aliyun.managed_credentials import ManagedCredentialsProvider
from server.aliyun import polardb_client


@dataclass
class FakeCredentials:
    expiration: int | None = 2_000
    access_key_id: str = "temporary-access-key"
    access_key_secret: str = "temporary-secret"
    security_token: str = "temporary-token"

    def get_expiration(self) -> int | None:
        return self.expiration


class FakeProvider:
    def get_provider_name(self) -> str:
        return "fake"

    def get_credentials(self) -> FakeCredentials:
        return FakeCredentials()

    async def get_credentials_async(self) -> FakeCredentials:
        return FakeCredentials()


class FailingProvider(FakeProvider):
    async def get_credentials_async(self) -> FakeCredentials:
        error = RuntimeError("fixed-secret from provider")
        error.code = "TEST1234567890ABCD"  # type: ignore[attr-defined]
        raise error


class CachedThenFailingProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def get_credentials_async(self) -> FakeCredentials:
        self.calls += 1
        if self.calls == 1:
            return FakeCredentials()
        raise RuntimeError("fixed-secret from provider")


class BlockingFailingProvider(FakeProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_credentials_async(self) -> FakeCredentials:
        self.started.set()
        await self.release.wait()
        raise RuntimeError("fixed-secret from provider")


@pytest.fixture(autouse=True)
def reset_credential_metric_sink():
    set_credential_metric_sink(None)
    yield
    set_credential_metric_sink(None)


async def test_refresh_emits_low_cardinality_sample() -> None:
    samples = []
    set_credential_metric_sink(samples.append)
    provider = ManagedCredentialsProvider(
        FakeProvider(),
        credential_mode="assume_role",
        clock=lambda: 1_000,
        refresh_jitter_seconds=(0, 0),
    )

    await provider.get_credentials_async()

    assert samples[0].name == "aliyun_credential_refresh"
    assert samples[0].fields == {
        "mode": "assume_role",
        "outcome": "success",
        "duration_seconds": pytest.approx(samples[0].fields["duration_seconds"]),
        "expires_in_seconds": 1_000.0,
    }
    assert "access_key" not in json.dumps(samples[0].fields).lower()
    assert "temporary-secret" not in json.dumps(samples[0].fields).lower()


async def test_request_failure_emits_only_safe_error_code() -> None:
    samples = []
    set_credential_metric_sink(samples.append)
    provider = ManagedCredentialsProvider(
        FailingProvider(),
        credential_mode="ecs_ram_role",
        clock=lambda: 1_000,
        refresh_jitter_seconds=(0, 0),
    )

    with pytest.raises(Exception):
        await provider.get_credentials_async()

    assert [sample.name for sample in samples] == [
        "aliyun_credential_refresh",
        "aliyun_credential_request_failure",
    ]
    assert samples[1].fields == {
        "mode": "ecs_ram_role",
        "error_code": "UNKNOWN",
    }
    assert "fixed-secret" not in json.dumps(
        [sample.fields for sample in samples]
    )


async def test_cached_refresh_fallback_does_not_emit_request_failure() -> None:
    samples = []
    set_credential_metric_sink(samples.append)
    provider = ManagedCredentialsProvider(
        CachedThenFailingProvider(),
        credential_mode="assume_role",
        clock=lambda: 1_000,
        refresh_jitter_seconds=(0, 0),
    )

    await provider.get_credentials_async()
    provider._refresh_at = 1_000
    await provider.get_credentials_async()

    assert [sample.name for sample in samples].count(
        "aliyun_credential_refresh"
    ) == 2
    assert "aliyun_credential_request_failure" not in [
        sample.name for sample in samples
    ]


async def test_shared_failed_request_emits_once_per_waiter() -> None:
    samples = []
    delegate = BlockingFailingProvider()
    provider = ManagedCredentialsProvider(
        delegate,
        credential_mode="assume_role",
        clock=lambda: 1_000,
        refresh_jitter_seconds=(0, 0),
    )
    set_credential_metric_sink(samples.append)

    owner = asyncio.create_task(provider.get_credentials_async())
    await delegate.started.wait()
    waiters = [
        asyncio.create_task(provider.get_credentials_async()) for _ in range(3)
    ]
    await asyncio.sleep(0)
    delegate.release.set()

    outcomes = await asyncio.gather(owner, *waiters, return_exceptions=True)

    assert all(isinstance(outcome, Exception) for outcome in outcomes)
    assert [sample.name for sample in samples].count(
        "aliyun_credential_refresh"
    ) == 1
    assert [sample.name for sample in samples].count(
        "aliyun_credential_request_failure"
    ) == 4


async def test_expiry_revalidation_emits_request_failure() -> None:
    now = 999
    samples = []
    provider = ManagedCredentialsProvider(
        FakeProvider(),
        credential_mode="assume_role",
        clock=lambda: now,
        refresh_jitter_seconds=(0, 0),
    )
    cached = FakeCredentials(expiration=1_000)
    shared: Future[FakeCredentials] = Future()
    provider._credentials = cached
    provider._refresh_at = 999
    provider._inflight = shared
    set_credential_metric_sink(samples.append)

    request = asyncio.create_task(provider.get_credentials_async())
    await asyncio.sleep(0)
    now = 1_000
    shared.set_result(cached)

    with pytest.raises(Exception):
        await request

    assert samples[-1].name == "aliyun_credential_request_failure"
    assert samples[-1].fields == {
        "mode": "assume_role",
        "error_code": "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
    }


async def test_cancelled_request_does_not_emit_request_failure() -> None:
    samples = []
    delegate = BlockingFailingProvider()
    provider = ManagedCredentialsProvider(
        delegate,
        credential_mode="assume_role",
        clock=lambda: 1_000,
        refresh_jitter_seconds=(0, 0),
    )
    set_credential_metric_sink(samples.append)

    request = asyncio.create_task(provider.get_credentials_async())
    await delegate.started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    delegate.release.set()
    await asyncio.sleep(0)

    assert "aliyun_credential_request_failure" not in [
        sample.name for sample in samples
    ]


def test_provider_rebuild_is_emitted_at_runtime_cache_boundary() -> None:
    class ActiveAliyun:
        config_revision = 1
        credential_digest = "digest"
        region_id = "cn-hangzhou"
        openapi_network = "public"
        credential_mode = "direct_ak"

        @staticmethod
        def has_active_credentials() -> bool:
            return True

    samples = []
    set_credential_metric_sink(samples.append)
    polardb_client.reset_polardb_client()
    try:
        with (
            patch(
                "server.aliyun.credential_provider.build_credential_provider",
                return_value=object(),
            ),
            patch(
                "server.aliyun.polardb_client_impl.AliyunPolarDBClient",
                return_value=object(),
            ),
        ):
            polardb_client._get_or_create_client(ActiveAliyun())
    finally:
        polardb_client.reset_polardb_client()

    assert samples[0].name == "aliyun_credential_provider_rebuild"
    assert samples[0].fields == {"mode": "direct_ak", "outcome": "success"}


def test_provider_construction_does_not_emit_rebuild_metric() -> None:
    samples = []
    set_credential_metric_sink(samples.append)

    from server.aliyun.credential_provider import DirectAKProvider

    DirectAKProvider(
        access_key_id="TEST1234567890ABCD",
        access_key_secret="fixed-secret",
    )

    assert samples == []


def test_runtime_provider_rebuild_failure_is_safe() -> None:
    class ActiveAliyun:
        config_revision = 1
        credential_digest = "digest"
        region_id = "cn-hangzhou"
        openapi_network = "public"
        credential_mode = "assume_role"

        @staticmethod
        def has_active_credentials() -> bool:
            return True

    samples = []
    set_credential_metric_sink(samples.append)
    polardb_client.reset_polardb_client()
    try:
        with patch(
            "server.aliyun.credential_provider.build_credential_provider",
            side_effect=RuntimeError("fixed-secret during rebuild"),
        ):
            with pytest.raises(RuntimeError):
                polardb_client._get_or_create_client(ActiveAliyun())
    finally:
        polardb_client.reset_polardb_client()

    assert samples[0].name == "aliyun_credential_provider_rebuild"
    assert samples[0].fields == {"mode": "assume_role", "outcome": "failure"}
    assert "fixed-secret" not in json.dumps(samples[0].fields)


def test_provider_rebuild_uses_guarded_sink(caplog) -> None:
    def failing_sink(_sample) -> None:
        raise RuntimeError("fixed-secret from metric sink")

    set_credential_metric_sink(failing_sink)
    with caplog.at_level(logging.INFO, logger="server.aliyun.credential_observability"):
        emit_credential_metric(
            CredentialMetricSample(
                name="aliyun_credential_provider_rebuild",
                fields={"mode": "direct_ak", "outcome": "success"},
            )
        )

    records = [
        record
        for record in caplog.records
        if getattr(record, "metric", None)
        == "aliyun_credential_provider_rebuild"
    ]
    assert records[-1].mode == "direct_ak"
    assert records[-1].outcome == "success"
    assert "fixed-secret" not in repr(records[-1].__dict__)
    assert "fixed-secret" not in caplog.text
