from __future__ import annotations

import abc
import logging
import secrets
import string
import warnings
from collections.abc import Mapping

from server.aliyun.diagnostics import safe_error_detail

logger = logging.getLogger(__name__)


class AliyunCredentialsUnavailable(RuntimeError):
    code = "ALIYUN_ACCESS_NOT_CONFIGURED"


class OpenAPIError(Exception):
    """Exception representing an Aliyun OpenAPI error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
    ):
        self.code = code
        self.request_id = request_id
        self.operation = operation
        self.message = safe_error_detail(message) or (
            "Alibaba Cloud OpenAPI request failed."
        )
        super().__init__(f"{code}: {self.message}")


OPENAPI_DUPLICATE_CODES = frozenset({
    "InvalidAccountName.Duplicate",
    "InvalidDBName.Duplicate",
})


class PolarDBClient(abc.ABC):
    """Abstract interface for PolarDB OpenAPI operations."""

    @abc.abstractmethod
    async def discover_clusters(self, region_id: str) -> list[dict]: ...

    @abc.abstractmethod
    async def describe_endpoints(self, cluster_id: str) -> dict: ...

    @abc.abstractmethod
    async def create_account(
        self,
        cluster_id: str,
        account_name: str,
        password: str,
        account_type: str = "Normal",
    ) -> dict: ...

    @abc.abstractmethod
    async def describe_account(
        self, cluster_id: str, account_name: str
    ) -> dict | None: ...

    @abc.abstractmethod
    async def create_agentic_db(self, settings: Mapping[str, str]) -> dict: ...

    @abc.abstractmethod
    async def delete_cluster(self, cluster_id: str) -> None: ...

    @abc.abstractmethod
    async def describe_cluster_attribute(self, cluster_id: str) -> dict: ...

    @abc.abstractmethod
    async def create_dedicated_cluster(
        self,
        params: Mapping[str, str],
        agentic_db_type: str,
        agentic_db_cluster_id: str | None,
        agentic_db_cluster_description: str,
        db_cluster_description: str,
    ) -> dict: ...

    @abc.abstractmethod
    async def create_database(
        self, cluster_id: str, db_name: str, account_name: str | None = None,
        character_set: str = "utf8", account_privilege: str = "ReadWrite",
    ) -> None: ...


class MockPolarDBClient(PolarDBClient):
    """Mock implementation for development and testing."""

    simulation_mode = True

    def __init__(self) -> None:
        self._clusters: dict[str, dict] = {}
        self._endpoints: dict[str, dict] = {}
        self._should_fail_create = False
        self._duplicate_errors: dict[str, bool] = {}
        self._purchases_by_token: dict[str, dict] = {}

    async def discover_clusters(self, region_id: str) -> list[dict]:
        return []

    async def describe_endpoints(self, cluster_id: str) -> dict:
        if cluster_id in self._endpoints:
            return self._endpoints[cluster_id]
        return {"items": [{"endpoint_type": "Primary", "address_items": [
            {"connection_string": "127.0.0.1", "port": "3306"},
        ]}]}

    async def create_account(
        self,
        cluster_id: str,
        account_name: str,
        password: str,
        account_type: str = "Normal",
    ) -> dict:
        if self._duplicate_errors.get("create_account"):
            self._duplicate_errors["create_account"] = False
            raise OpenAPIError("InvalidAccountName.Duplicate", "Account already exists")
        return {
            "account_name": account_name,
            "account_type": account_type,
            "status": "available",
        }

    async def describe_account(
        self, cluster_id: str, account_name: str
    ) -> dict | None:
        del cluster_id
        return {
            "account_name": account_name,
            "account_type": "Normal",
            "status": "Available",
        }

    async def create_agentic_db(self, settings: Mapping[str, str]) -> dict:
        if self._should_fail_create:
            self._should_fail_create = False
            raise OpenAPIError("OperationDenied", "Mock create failure")
        cluster_id = "pc-mock-" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        self._clusters[cluster_id] = {"status": "Creating"}
        return {
            "cluster_id": cluster_id,
            "agentic_db_cluster_id": "pagc-mock-" + "".join(
                secrets.choice(string.ascii_lowercase + string.digits)
                for _ in range(8)
            ),
        }

    async def create_dedicated_cluster(
        self,
        params: Mapping[str, str],
        agentic_db_type: str,
        agentic_db_cluster_id: str | None,
        agentic_db_cluster_description: str,
        db_cluster_description: str,
    ) -> dict:
        if self._should_fail_create:
            self._should_fail_create = False
            raise OpenAPIError("OperationDenied", "Mock create failure")
        client_token = params.get("client_token")
        if client_token and client_token in self._purchases_by_token:
            return dict(self._purchases_by_token[client_token])
        cluster_id = "pc-mock-" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        self._clusters[cluster_id] = {"status": "Creating"}
        returned_agentic_id = agentic_db_cluster_id or (
            "pagc-mock-" + "".join(
                secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
            )
        )
        result = {
            "cluster_id": cluster_id,
            "agentic_db_cluster_id": returned_agentic_id,
            "agentic_db_cluster_description": agentic_db_cluster_description,
            "request_id": "request-mock-" + cluster_id.removeprefix("pc-mock-"),
        }
        if client_token:
            self._purchases_by_token[client_token] = dict(result)
        return result

    async def delete_cluster(self, cluster_id: str) -> None:
        self._clusters.pop(cluster_id, None)
        logger.info("Mock: deleted cluster %s", cluster_id)

    async def describe_cluster_attribute(self, cluster_id: str) -> dict:
        info = self._clusters.get(cluster_id, {"status": "Creating"})
        return {"status": info["status"]}

    async def create_database(
        self, cluster_id: str, db_name: str, account_name: str | None = None,
        character_set: str = "utf8", account_privilege: str = "ReadWrite",
    ) -> None:
        if self._duplicate_errors.get("create_database"):
            self._duplicate_errors["create_database"] = False
            raise OpenAPIError("InvalidDBName.Duplicate", "Database already exists")

    # -- Test control knobs --

    def set_create_failure(self, should_fail: bool) -> None:
        self._should_fail_create = should_fail

    def advance_to_running(self, cluster_id: str) -> None:
        self._clusters[cluster_id] = {"status": "Running"}

    def set_endpoint_data(self, cluster_id: str, host: str, port: int) -> None:
        self._endpoints[cluster_id] = {"items": [{"endpoint_type": "Primary", "address_items": [
            {
                "connection_string": host,
                "port": str(port),
                "net_type": "Private",
            },
        ]}]}

    def set_duplicate_error(self, method: str, should_duplicate: bool) -> None:
        self._duplicate_errors[method] = should_duplicate


_client: PolarDBClient | None = None
_CacheKey = tuple[int, str, str, str]
_client_credential_hash: _CacheKey | str | None = None


def _effective_client_key(aliyun, *, simulation_enabled: bool) -> _CacheKey | str:
    if not aliyun.has_active_credentials():
        if simulation_enabled:
            return "simulation"
        raise AliyunCredentialsUnavailable(
            "Alibaba Cloud access is not configured"
        )
    return (
        aliyun.config_revision,
        aliyun.credential_digest,
        aliyun.region_id,
        aliyun.openapi_network,
    )


def _get_or_create_client(
    aliyun,
    *,
    simulation_enabled: bool = False,
) -> PolarDBClient:
    """Return one client/provider per effective runtime credential key."""
    global _client, _client_credential_hash

    # Test overrides intentionally bypass runtime configuration selection.
    if _client is not None and _client_credential_hash == "__test_override__":
        return _client

    cache_key = _effective_client_key(
        aliyun,
        simulation_enabled=simulation_enabled,
    )
    if _client is not None and cache_key == _client_credential_hash:
        return _client

    if aliyun.has_active_credentials():
        from server.aliyun.credential_provider import build_credential_provider
        from server.aliyun.credential_observability import (
            CredentialMetricSample,
            emit_credential_metric,
        )
        from server.aliyun.polardb_client_impl import AliyunPolarDBClient

        mode = getattr(aliyun, "credential_mode", "unknown")
        try:
            _client = AliyunPolarDBClient(build_credential_provider(aliyun))
        except Exception:
            emit_credential_metric(
                CredentialMetricSample(
                    name="aliyun_credential_provider_rebuild",
                    fields={"mode": mode, "outcome": "failure"},
                )
            )
            raise
        emit_credential_metric(
            CredentialMetricSample(
                name="aliyun_credential_provider_rebuild",
                fields={"mode": mode, "outcome": "success"},
            )
        )
    elif simulation_enabled:
        _client = MockPolarDBClient()
    else:  # pragma: no cover - guarded by _effective_client_key
        raise AliyunCredentialsUnavailable(
            "Alibaba Cloud access is not configured"
        )
    _client_credential_hash = cache_key
    return _client


def get_polardb_client() -> PolarDBClient:
    warnings.warn(
        "get_polardb_client() is deprecated, use get_polardb_client_async() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from server.config import get_config

    config = get_config()
    return _get_or_create_client(
        config.aliyun,
        simulation_enabled=(
            config.polardb.tenant_provisioning
            .dedicated_pool_simulation_enabled
        ),
    )


async def get_polardb_client_async(session) -> PolarDBClient:
    """Return a cached PolarDBClient, rebuilding when credentials change."""
    from server.config import get_config

    config = get_config()
    return _get_or_create_client(
        config.aliyun,
        simulation_enabled=(
            config.polardb.tenant_provisioning
            .dedicated_pool_simulation_enabled
        ),
    )


def set_polardb_client(client: PolarDBClient) -> None:
    global _client, _client_credential_hash
    _client = client
    _client_credential_hash = "__test_override__"


def reset_polardb_client() -> None:
    global _client, _client_credential_hash
    _client = None
    _client_credential_hash = None
