from __future__ import annotations

import asyncio
import tomllib
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from server.aliyun import credential_provider
from server.aliyun.credential_provider import (
    AssumeRoleProvider,
    DirectAKProvider,
    ECSRamRoleProvider,
    build_credential_provider,
)
from server.aliyun.managed_credentials import (
    ManagedCredentialsProvider,
    TemporaryCredentialExpired,
)
from server.config import AliyunConfig


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeCredentials:
    expiration: int | None
    provider_name: str = "fake"
    access_key_id: str = "temporary-ak"
    access_key_secret: str = "temporary-secret"
    security_token: str = "temporary-token"

    def get_expiration(self) -> int | None:
        return self.expiration

    def get_provider_name(self) -> str:
        return self.provider_name

    def get_access_key_id(self) -> str:
        return self.access_key_id

    def get_access_key_secret(self) -> str:
        return self.access_key_secret

    def get_security_token(self) -> str:
        return self.security_token


class FakeProvider:
    def __init__(self, credentials: FakeCredentials | None = None) -> None:
        self.credentials = credentials or FakeCredentials(expiration=None)

    def get_credentials(self) -> FakeCredentials:
        return self.credentials

    async def get_credentials_async(self) -> FakeCredentials:
        return self.credentials

    def get_provider_name(self) -> str:
        return self.credentials.provider_name


class SequencedProvider:
    def __init__(self, *results: FakeCredentials | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    def _next(self) -> FakeCredentials:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_credentials(self) -> FakeCredentials:
        return self._next()

    async def get_credentials_async(self) -> FakeCredentials:
        await asyncio.sleep(0)
        return self._next()

    def get_provider_name(self) -> str:
        return "sequence"


class BlockingFailureProvider:
    def __init__(self, valid: FakeCredentials) -> None:
        self.valid = valid
        self.calls = 0
        self.async_calls = 0
        self.fail_refresh = False
        self.sync_started = Event()
        self.sync_release = Event()
        self.async_started: asyncio.Event | None = None
        self.async_release: asyncio.Event | None = None

    def get_credentials(self) -> FakeCredentials:
        self.calls += 1
        if not self.fail_refresh:
            return self.valid
        self.sync_started.set()
        if not self.sync_release.wait(timeout=2):
            raise AssertionError("sync refresh was not released")
        raise RuntimeError("metadata unavailable")

    async def get_credentials_async(self) -> FakeCredentials:
        self.async_calls += 1
        if not self.fail_refresh:
            return self.valid
        assert self.async_started is not None
        assert self.async_release is not None
        self.async_started.set()
        await self.async_release.wait()
        raise RuntimeError("metadata unavailable")

    def get_provider_name(self) -> str:
        return "blocking"


class BlockingSuccessProvider:
    def __init__(self, credentials: FakeCredentials) -> None:
        self.credentials = credentials
        self.sync_calls = 0
        self.async_calls = 0
        self.sync_started = Event()
        self.sync_release = Event()
        self.async_started: asyncio.Event | None = None
        self.async_release: asyncio.Event | None = None

    def get_credentials(self) -> FakeCredentials:
        self.sync_calls += 1
        self.sync_started.set()
        if not self.sync_release.wait(timeout=2):
            raise AssertionError("sync refresh was not released")
        return self.credentials

    async def get_credentials_async(self) -> FakeCredentials:
        self.async_calls += 1
        assert self.async_started is not None
        assert self.async_release is not None
        self.async_started.set()
        await self.async_release.wait()
        return self.credentials

    def get_provider_name(self) -> str:
        return "blocking-success"


class InterruptingProvider:
    def get_credentials(self) -> FakeCredentials:
        raise KeyboardInterrupt()

    async def get_credentials_async(self) -> FakeCredentials:
        raise KeyboardInterrupt()

    def get_provider_name(self) -> str:
        return "interrupting"


class _HostileDiagnosticsError(Exception):
    @property
    def code(self):
        raise RuntimeError("hostile diagnostic accessor")


class _BlockingHostileProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def get_credentials(self):
        raise _HostileDiagnosticsError()

    async def get_credentials_async(self):
        self.started.set()
        await self.release.wait()
        raise _HostileDiagnosticsError()

    def get_provider_name(self) -> str:
        return "hostile"


class FakeClock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def set(self, now: int) -> None:
        self.now = now


def test_credentials_sdk_is_a_direct_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "alibabacloud-credentials>=1.0.8,<2.0.0" in project["project"]["dependencies"]


def test_ecs_provider_forces_imdsv2_and_disables_sdk_background_updates(monkeypatch) -> None:
    created: dict[str, object] = {}
    monkeypatch.setattr(
        credential_provider,
        "EcsRamRoleCredentialsProvider",
        lambda **kwargs: created.update(kwargs) or FakeProvider(),
    )

    provider = ECSRamRoleProvider(
        role_name=None,
        region_id="cn-hangzhou",
        openapi_network="vpc",
    )

    assert created["disable_imds_v1"] is True
    assert created["async_update_enabled"] is False
    assert created["http_options"].proxy is None
    assert provider.mode == "ecs_ram_role"
    assert provider.region_id == "cn-hangzhou"
    assert provider.openapi_network == "vpc"


def test_assume_role_uses_explicit_official_provider_and_resolved_endpoint(monkeypatch) -> None:
    created: dict[str, object] = {}
    source = FakeProvider()
    raw = FakeProvider()
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "ambient-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ambient-sk")
    monkeypatch.setenv("ALIBABA_CLOUD_ROLE_ARN", "acs:ram::1:role/ambient")
    monkeypatch.setenv("ALIBABA_CLOUD_ROLE_SESSION_NAME", "ambient-session")
    monkeypatch.setattr(
        credential_provider,
        "StaticAKCredentialsProvider",
        lambda **kwargs: created.setdefault("source", kwargs) and source,
    )
    monkeypatch.setattr(
        credential_provider,
        "SafeRamRoleArnCredentialsProvider",
        lambda **kwargs: created.update(kwargs) or raw,
    )

    provider = AssumeRoleProvider(
        source_access_key_id="source-ak",
        source_access_key_secret="source-sk",
        role_arn="acs:ram::1234567890123456:role/pas-runtime",
        role_session_name="pas-session",
        duration_seconds=1800,
        external_id="external-id",
        region_id="cn-beijing",
        openapi_network="vpc",
    )

    assert created["source"] == {
        "access_key_id": "source-ak",
        "access_key_secret": "source-sk",
    }
    assert created["credentials_provider"] is source
    assert created["role_arn"] == "acs:ram::1234567890123456:role/pas-runtime"
    assert created["role_session_name"] == "pas-session"
    assert created["duration_seconds"] == 1800
    assert created["external_id"] == "external-id"
    assert created["sts_endpoint"] == "sts-vpc.cn-beijing.aliyuncs.com"
    assert created["http_options"].proxy is None
    assert provider.mode == "assume_role"
    assert provider.credential_client.cloud_credential.provider is provider.managed_provider


def test_direct_provider_constructs_explicit_static_credentials(monkeypatch) -> None:
    created: dict[str, object] = {}
    raw = FakeProvider(FakeCredentials(expiration=None, provider_name="static_ak"))
    monkeypatch.setattr(
        credential_provider,
        "StaticAKCredentialsProvider",
        lambda **kwargs: created.update(kwargs) or raw,
    )

    provider = DirectAKProvider(
        access_key_id="ak",
        access_key_secret="sk",
        region_id="cn-shanghai",
        openapi_network="public",
    )

    assert created == {"access_key_id": "ak", "access_key_secret": "sk"}
    assert provider.mode == "direct_ak"
    assert provider.probe().expires_at is None
    assert provider.probe().provider_name == "static_ak"


def test_direct_provider_rejects_blank_config_instead_of_using_ambient_keys(monkeypatch) -> None:
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "ambient-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ambient-sk")

    with pytest.raises(ValueError, match="access_key_id"):
        DirectAKProvider(access_key_id="", access_key_secret="configured-sk")
    with pytest.raises(ValueError, match="access_key_secret"):
        DirectAKProvider(access_key_id="configured-ak", access_key_secret="")

    provider = DirectAKProvider(
        access_key_id="configured-ak",
        access_key_secret="configured-sk",
    )
    credentials = provider.managed_provider.get_credentials()
    assert credentials.get_access_key_id() == "configured-ak"
    assert credentials.get_access_key_secret() == "configured-sk"


def test_assume_role_rejects_blank_config_instead_of_using_ambient_values(monkeypatch) -> None:
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "ambient-ak")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ambient-sk")
    monkeypatch.setenv("ALIBABA_CLOUD_ROLE_ARN", "acs:ram::1:role/ambient")
    monkeypatch.setenv("ALIBABA_CLOUD_ROLE_SESSION_NAME", "ambient-session")

    valid = {
        "source_access_key_id": "configured-ak",
        "source_access_key_secret": "configured-sk",
        "role_arn": "acs:ram::1234567890123456:role/pas-runtime",
        "role_session_name": "configured-session",
    }
    for field in valid:
        invalid = {**valid, field: ""}
        with pytest.raises(ValueError, match=field):
            AssumeRoleProvider(**invalid)


def test_ecs_omitted_role_ignores_ambient_role_override(monkeypatch) -> None:
    monkeypatch.setenv("ALIBABA_CLOUD_ECS_METADATA", "ambient-role")

    provider = ECSRamRoleProvider(role_name=None)

    assert provider.managed_provider._delegate._role_name is None


async def test_managed_provider_has_async_singleflight_refresh() -> None:
    delegate = SequencedProvider(FakeCredentials(expiration=2000))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    credentials = await asyncio.gather(
        *(managed.get_credentials_async() for _ in range(12))
    )

    assert delegate.calls == 1
    assert all(result is credentials[0] for result in credentials)


def test_managed_provider_has_sync_singleflight_refresh() -> None:
    delegate = SequencedProvider(FakeCredentials(expiration=2000))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    first = managed.get_credentials()
    second = managed.get_credentials()

    assert delegate.calls == 1
    assert first is second


async def test_managed_provider_never_returns_expired_cache() -> None:
    clock = FakeClock(999)
    delegate = SequencedProvider(
        FakeCredentials(expiration=1000),
        RuntimeError("metadata unavailable"),
    )
    managed = ManagedCredentialsProvider(
        delegate,
        clock=clock,
        refresh_jitter_seconds=(0, 0),
    )

    await managed.get_credentials_async()
    clock.set(1001)

    with pytest.raises(TemporaryCredentialExpired):
        await managed.get_credentials_async()


def test_managed_provider_uses_still_valid_cache_after_refresh_failure() -> None:
    clock = FakeClock(1000)
    cached = FakeCredentials(expiration=2000)
    delegate = SequencedProvider(cached, RuntimeError("metadata unavailable"))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=clock,
        refresh_jitter_seconds=(0, 0),
    )
    assert managed.get_credentials() is cached

    managed._refresh_at = 1000

    assert managed.get_credentials() is cached
    assert delegate.calls == 2


def test_sync_failed_refresh_collapses_waiters_until_bounded_retry() -> None:
    clock = FakeClock(1000)
    delegate = BlockingFailureProvider(FakeCredentials(expiration=2000))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=clock,
        refresh_jitter_seconds=(0, 0),
        failure_retry_seconds=60,
    )
    cached = managed.get_credentials()
    managed._refresh_at = 1000
    delegate.fail_refresh = True

    with ThreadPoolExecutor(max_workers=8) as executor:
        leader = executor.submit(managed.get_credentials)
        assert delegate.sync_started.wait(timeout=2)
        waiters = [executor.submit(managed.get_credentials) for _ in range(7)]
        delegate.sync_release.set()
        results = [leader.result(), *(waiter.result() for waiter in waiters)]

    assert delegate.calls == 2
    assert all(result is cached for result in results)


async def test_async_failed_refresh_collapses_waiters_until_bounded_retry() -> None:
    clock = FakeClock(1000)
    delegate = BlockingFailureProvider(FakeCredentials(expiration=2000))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=clock,
        refresh_jitter_seconds=(0, 0),
        failure_retry_seconds=60,
    )
    cached = await managed.get_credentials_async()
    managed._refresh_at = 1000
    delegate.fail_refresh = True
    delegate.async_started = asyncio.Event()
    delegate.async_release = asyncio.Event()

    leader = asyncio.create_task(managed.get_credentials_async())
    await asyncio.wait_for(delegate.async_started.wait(), timeout=2)
    waiters = [asyncio.create_task(managed.get_credentials_async()) for _ in range(7)]
    delegate.async_release.set()
    results = await asyncio.gather(leader, *waiters)

    assert delegate.calls == 0
    assert delegate.async_calls == 2
    assert all(result is cached for result in results)


async def test_hostile_diagnostics_cannot_orphan_shared_failure_future() -> None:
    delegate = _BlockingHostileProvider()
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    owner = asyncio.create_task(managed.get_credentials_async())
    await asyncio.wait_for(delegate.started.wait(), timeout=1)
    waiter = asyncio.create_task(managed.get_credentials_async())
    delegate.release.set()

    with pytest.raises(TemporaryCredentialExpired):
        await asyncio.wait_for(owner, timeout=1)
    with pytest.raises(TemporaryCredentialExpired):
        await asyncio.wait_for(waiter, timeout=1)


async def test_mixed_sync_and_async_refreshes_share_one_inflight_attempt() -> None:
    clock = FakeClock(1000)
    delegate = BlockingFailureProvider(FakeCredentials(expiration=2000))
    managed = ManagedCredentialsProvider(
        delegate,
        clock=clock,
        refresh_jitter_seconds=(0, 0),
        failure_retry_seconds=60,
    )
    cached = managed.get_credentials()
    managed._refresh_at = 1000
    delegate.fail_refresh = True

    sync_refresh = asyncio.create_task(asyncio.to_thread(managed.get_credentials))
    await asyncio.to_thread(delegate.sync_started.wait, 2)
    async_waiter = asyncio.create_task(managed.get_credentials_async())
    delegate.sync_release.set()

    sync_result, async_result = await asyncio.gather(sync_refresh, async_waiter)

    assert delegate.calls == 2
    assert delegate.async_calls == 0
    assert sync_result is cached
    assert async_result is cached


async def test_cancelled_async_owner_does_not_orphan_sync_waiter() -> None:
    credentials = FakeCredentials(expiration=2000)
    delegate = BlockingSuccessProvider(credentials)
    delegate.async_started = asyncio.Event()
    delegate.async_release = asyncio.Event()
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    owner = asyncio.create_task(managed.get_credentials_async())
    await asyncio.wait_for(delegate.async_started.wait(), timeout=2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        sync_waiter = executor.submit(managed.get_credentials)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        delegate.async_release.set()
        try:
            assert await asyncio.to_thread(sync_waiter.result, 0.1) is credentials
        finally:
            if managed._inflight is not None and not managed._inflight.done():
                managed._inflight.set_result(credentials)
            await asyncio.to_thread(sync_waiter.result, 2)

    assert await managed.get_credentials_async() is credentials
    assert delegate.async_calls == 1
    assert delegate.sync_calls == 0


async def test_cancelled_async_waiter_does_not_poison_sync_owner(monkeypatch) -> None:
    credentials = FakeCredentials(expiration=2000)
    delegate = BlockingSuccessProvider(credentials)
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    sync_owner = asyncio.create_task(asyncio.to_thread(managed.get_credentials))
    await asyncio.to_thread(delegate.sync_started.wait, 2)
    waiting = asyncio.Event()
    original_wait = managed._await_shared_future

    async def wait_until_cancelled(future):
        waiting.set()
        return await original_wait(future)

    monkeypatch.setattr(managed, "_await_shared_future", wait_until_cancelled)
    async_waiter = asyncio.create_task(managed.get_credentials_async())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    async_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await async_waiter
    delegate.sync_release.set()

    assert await sync_owner is credentials
    assert await managed.get_credentials_async() is credentials
    assert delegate.sync_calls == 1
    assert delegate.async_calls == 0


async def test_waiter_revalidates_fallback_after_future_completion(monkeypatch) -> None:
    clock = FakeClock(1000)
    cached = FakeCredentials(expiration=1001)
    managed = ManagedCredentialsProvider(
        FakeProvider(cached),
        clock=clock,
        refresh_jitter_seconds=(0, 0),
    )
    shared: Future[FakeCredentials] = Future()
    managed._credentials = cached
    managed._refresh_at = 1000
    managed._inflight = shared
    waiting = asyncio.Event()
    original_wait = managed._await_shared_future

    async def wait_until_released(future):
        waiting.set()
        return await original_wait(future)

    monkeypatch.setattr(managed, "_await_shared_future", wait_until_released)
    waiter = asyncio.create_task(managed.get_credentials_async())
    await asyncio.wait_for(waiting.wait(), timeout=2)
    shared.set_result(cached)
    clock.set(1001)

    with pytest.raises(TemporaryCredentialExpired):
        await waiter


async def test_async_owner_completes_when_default_executor_has_sync_waiter(monkeypatch) -> None:
    credentials = FakeCredentials(expiration=2000)
    delegate = BlockingSuccessProvider(credentials)
    delegate.async_started = asyncio.Event()
    delegate.async_release = asyncio.Event()
    managed = ManagedCredentialsProvider(
        delegate,
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    original_complete = managed._complete_success

    def complete_once(future, value):
        if not future.done():
            original_complete(future, value)

    monkeypatch.setattr(managed, "_complete_success", complete_once)
    waiter_entered = Event()
    original_begin = managed._begin_refresh

    def begin_waiter():
        waiter_entered.set()
        return original_begin()

    try:
        owner = asyncio.create_task(managed.get_credentials_async())
        await asyncio.wait_for(delegate.async_started.wait(), timeout=2)
        monkeypatch.setattr(managed, "_begin_refresh", begin_waiter)
        sync_waiter = asyncio.create_task(asyncio.to_thread(managed.get_credentials))
        for _ in range(100):
            if waiter_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert waiter_entered.is_set()
        delegate.async_release.set()

        owner_result, waiter_result = await asyncio.wait_for(
            asyncio.gather(owner, sync_waiter),
            timeout=0.5,
        )
        assert owner_result is credentials
        assert waiter_result is credentials
    finally:
        delegate.async_release.set()
        if managed._inflight is not None and not managed._inflight.done():
            managed._inflight.set_result(credentials)
        executor.shutdown(wait=False, cancel_futures=True)


def test_sync_keyboard_interrupt_propagates() -> None:
    managed = ManagedCredentialsProvider(
        InterruptingProvider(),
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )

    with pytest.raises(KeyboardInterrupt):
        managed.get_credentials()


def test_probe_exposes_only_metadata() -> None:
    credentials = FakeCredentials(expiration=2000, provider_name="ecs_ram_role")
    provider = ECSRamRoleProvider.__new__(ECSRamRoleProvider)
    provider.mode = "ecs_ram_role"
    provider.region_id = "cn-hangzhou"
    provider.openapi_network = "public"
    provider.role_name = "pas-runtime"
    provider.managed_provider = ManagedCredentialsProvider(
        FakeProvider(credentials),
        clock=FakeClock(1000),
        refresh_jitter_seconds=(0, 0),
    )
    provider.managed_provider.get_credentials()

    probe = provider.probe()

    assert probe.mode == "ecs_ram_role"
    assert probe.provider_name == "ecs_ram_role"
    assert probe.expires_at == 2000
    assert probe.role_name == "pas-runtime"
    assert "temporary-secret" not in repr(probe)


def test_build_credential_provider_selects_direct_and_assume_role_modes() -> None:
    direct = build_credential_provider(
        AliyunConfig(
            credential_mode="direct_ak",
            access_key_id="ak",
            access_key_secret="sk",
        )
    )
    assume = build_credential_provider(
        AliyunConfig(
            credential_mode="assume_role",
            access_key_id="source-ak",
            access_key_secret="source-sk",
            role_arn="acs:ram::1234567890123456:role/pas-runtime",
        )
    )

    assert isinstance(direct, DirectAKProvider)
    assert isinstance(assume, AssumeRoleProvider)
