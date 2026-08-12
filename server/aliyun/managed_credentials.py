from __future__ import annotations

import asyncio
import random
import threading
import time
from concurrent.futures import Future
from typing import Callable

from alibabacloud_credentials.provider.refreshable import Credentials
from alibabacloud_credentials_api import ICredentialsProvider

from server.aliyun.diagnostics import error_code_from_error, request_id_from_error
from server.aliyun.credential_observability import (
    CredentialMetricSample,
    emit_credential_metric,
)


class TemporaryCredentialExpired(RuntimeError):
    """Raised when temporary credentials cannot be refreshed before expiry."""


class CredentialProviderFailure(TemporaryCredentialExpired):
    """Sanitized provider failure retaining only structured SDK diagnostics."""

    def __init__(self, error: BaseException) -> None:
        super().__init__("credential provider refresh failed")
        try:
            self.code = error_code_from_error(error)
            self.request_id = request_id_from_error(error)
        except Exception:
            self.code = None
            self.request_id = None


class ManagedCredentialsProvider(ICredentialsProvider):
    """Refresh an official provider once at a time without serving expiry."""

    _SDK_STALE_SECONDS = 15 * 60

    def __init__(
        self,
        delegate: ICredentialsProvider,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
        refresh_jitter_seconds: tuple[int, int] = (0, 120),
        failure_retry_seconds: int = 30,
        credential_mode: str = "unknown",
    ) -> None:
        self._delegate = delegate
        self._clock = clock
        self._jitter_seconds = random.randint(*refresh_jitter_seconds)
        self._failure_retry_seconds = failure_retry_seconds
        self._credential_mode = credential_mode
        self._credentials: Credentials | None = None
        self._refresh_at = 0
        self._next_retry_at = 0
        self._state_lock = threading.Lock()
        self._inflight: Future[Credentials] | None = None
        self._refresh_started_at: float | None = None
        self._async_tasks: set[asyncio.Task[Credentials]] = set()

    def get_provider_name(self) -> str:
        return self._delegate.get_provider_name()

    @property
    def cached_credentials(self) -> Credentials | None:
        return self._credentials

    def get_credentials(self) -> Credentials:
        cached, future, owner = self._begin_refresh()
        if cached is not None:
            return cached
        assert future is not None
        if owner:
            try:
                self._complete_success(future, self._delegate.get_credentials())
            except Exception as error:
                self._complete_failure(future, error)
        try:
            return self._revalidate_result(future.result())
        except Exception as error:
            self._emit_request_failure(error)
            raise

    async def get_credentials_async(self) -> Credentials:
        task = asyncio.create_task(self._run_async_attempt())
        self._async_tasks.add(task)
        task.add_done_callback(self._discard_async_task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_request_failure(error)
            raise

    async def _run_async_attempt(self) -> Credentials:
        cached, future, owner = self._begin_refresh()
        if cached is not None:
            return cached
        assert future is not None
        if owner:
            try:
                credentials = await self._delegate.get_credentials_async()
                self._complete_success(future, credentials)
            except asyncio.CancelledError as error:
                self._complete_failure(future, error)
                raise
            except Exception as error:
                self._complete_failure(future, error)
        result = await self._await_shared_future(future)
        return self._revalidate_result(result)

    async def _await_shared_future(
        self,
        future: Future[Credentials],
    ) -> Credentials:
        return await asyncio.shield(asyncio.wrap_future(future))

    def _discard_async_task(self, task: asyncio.Task[Credentials]) -> None:
        self._async_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _begin_refresh(
        self,
    ) -> tuple[Credentials | None, Future[Credentials] | None, bool]:
        with self._state_lock:
            cached = self._valid_cached(self._clock())
            if cached is not None and not self._refresh_due(self._clock()):
                return cached, None, False
            if self._inflight is not None:
                return None, self._inflight, False
            future: Future[Credentials] = Future()
            self._inflight = future
            self._refresh_started_at = time.monotonic()
            return None, future, True

    def _refresh_due(self, now: int) -> bool:
        if self._credentials is None:
            return True
        expiration = self._credentials.get_expiration()
        if expiration is None:
            return False
        if expiration <= now:
            return True
        return now >= self._refresh_at and now >= self._next_retry_at

    def _complete_success(
        self,
        future: Future[Credentials],
        credentials: Credentials,
    ) -> None:
        now = self._clock()
        expiration = credentials.get_expiration()
        if expiration is not None and expiration <= now:
            raise TemporaryCredentialExpired(
                "credential provider returned an expired temporary credential"
            )
        with self._state_lock:
            self._credentials = credentials
            self._next_retry_at = 0
            if expiration is None:
                self._refresh_at = 2**63 - 1
            else:
                sdk_stale_time = expiration - self._SDK_STALE_SECONDS
                self._refresh_at = min(
                    expiration - 1,
                    sdk_stale_time + self._jitter_seconds,
                )
            self._inflight = None
            refresh_started_at = self._refresh_started_at
            self._refresh_started_at = None
            future.set_result(credentials)
        fields: dict[str, str | float] = {
            "mode": self._credential_mode,
            "outcome": "success",
            "duration_seconds": time.monotonic()
            - (refresh_started_at or time.monotonic()),
        }
        if expiration is not None:
            fields["expires_in_seconds"] = float(expiration - now)
        emit_credential_metric(
            CredentialMetricSample(
                name="aliyun_credential_refresh", fields=fields
            )
        )

    def _complete_failure(
        self,
        future: Future[Credentials],
        error: BaseException,
    ) -> None:
        failure = CredentialProviderFailure(error)
        with self._state_lock:
            cached = self._valid_cached(self._clock())
            self._inflight = None
            refresh_started_at = self._refresh_started_at
            self._refresh_started_at = None
            if cached is not None:
                self._next_retry_at = self._clock() + self._failure_retry_seconds
                future.set_result(cached)
            else:
                future.set_exception(failure)
        error_code = failure.code or "UNKNOWN"
        emit_credential_metric(
            CredentialMetricSample(
                name="aliyun_credential_refresh",
                fields={
                    "mode": self._credential_mode,
                    "outcome": "failure",
                    "error_code": error_code,
                    "duration_seconds": time.monotonic()
                    - (refresh_started_at or time.monotonic()),
                },
            )
        )

    def _emit_request_failure(self, error: BaseException) -> None:
        error_code = "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED"
        if isinstance(error, CredentialProviderFailure):
            error_code = error.code
        elif not isinstance(error, TemporaryCredentialExpired):
            error_code = getattr(error, "code", None)
        emit_credential_metric(
            CredentialMetricSample(
                name="aliyun_credential_request_failure",
                fields={
                    "mode": self._credential_mode,
                    "error_code": error_code,
                },
            )
        )

    def _valid_cached(self, now: int) -> Credentials | None:
        if self._credentials is None:
            return None
        expiration = self._credentials.get_expiration()
        if expiration is not None and expiration <= now:
            return None
        return self._credentials

    def _revalidate_result(self, credentials: Credentials) -> Credentials:
        expiration = credentials.get_expiration()
        if expiration is not None and expiration <= self._clock():
            raise TemporaryCredentialExpired(
                "temporary credentials expired while waiting for refresh"
            )
        return credentials
