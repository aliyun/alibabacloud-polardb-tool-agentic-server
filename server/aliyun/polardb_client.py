from __future__ import annotations

import abc
import hashlib
import logging
import secrets
import string
import warnings
from collections.abc import Mapping

logger = logging.getLogger(__name__)


class OpenAPIError(Exception):
    """Exception representing an Aliyun OpenAPI error response."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


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
    async def create_account(self, cluster_id: str, account_name: str, password: str) -> dict: ...

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
        self, cluster_id: str, db_name: str, account_name: str,
        character_set: str = "utf8", account_privilege: str = "ReadWrite",
    ) -> None: ...


class MockPolarDBClient(PolarDBClient):
    """Mock implementation for development and testing."""

    def __init__(self) -> None:
        self._clusters: dict[str, dict] = {}
        self._endpoints: dict[str, dict] = {}
        self._should_fail_create = False
        self._duplicate_errors: dict[str, bool] = {}

    async def discover_clusters(self, region_id: str) -> list[dict]:
        return []

    async def describe_endpoints(self, cluster_id: str) -> dict:
        if cluster_id in self._endpoints:
            return self._endpoints[cluster_id]
        return {"items": [{"endpoint_type": "Primary", "address_items": [
            {"connection_string": "127.0.0.1", "port": "3306"},
        ]}]}

    async def create_account(self, cluster_id: str, account_name: str, password: str) -> dict:
        if self._duplicate_errors.get("create_account"):
            self._duplicate_errors["create_account"] = False
            raise OpenAPIError("InvalidAccountName.Duplicate", "Account already exists")
        return {"account_name": account_name, "status": "available"}

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
        cluster_id = "pc-mock-" + "".join(
            secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
        )
        self._clusters[cluster_id] = {"status": "Creating"}
        returned_agentic_id = agentic_db_cluster_id or (
            "pagc-mock-" + "".join(
                secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)
            )
        )
        return {
            "cluster_id": cluster_id,
            "agentic_db_cluster_id": returned_agentic_id,
            "agentic_db_cluster_description": agentic_db_cluster_description,
        }

    async def delete_cluster(self, cluster_id: str) -> None:
        self._clusters.pop(cluster_id, None)
        logger.info("Mock: deleted cluster %s", cluster_id)

    async def describe_cluster_attribute(self, cluster_id: str) -> dict:
        info = self._clusters.get(cluster_id, {"status": "Creating"})
        return {"status": info["status"]}

    async def create_database(
        self, cluster_id: str, db_name: str, account_name: str,
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
            {"connection_string": host, "port": str(port)},
        ]}]}

    def set_duplicate_error(self, method: str, should_duplicate: bool) -> None:
        self._duplicate_errors[method] = should_duplicate


_client: PolarDBClient | None = None
_client_credential_hash: str | None = None


def get_polardb_client() -> PolarDBClient:
    warnings.warn(
        "get_polardb_client() is deprecated, use get_polardb_client_async() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    global _client
    if _client is None:
        from server.config import get_config

        config = get_config()
        if config.aliyun.access_key_id and config.aliyun.access_key_secret:
            from server.aliyun.credential_provider import DirectAKProvider
            from server.aliyun.polardb_client_impl import AliyunPolarDBClient

            provider = DirectAKProvider(
                ak=config.aliyun.access_key_id,
                sk=config.aliyun.access_key_secret,
                region_id=config.aliyun.region_id,
                openapi_network=config.aliyun.openapi_network,
            )
            _client = AliyunPolarDBClient(provider)
        else:
            _client = MockPolarDBClient()
    return _client


async def get_polardb_client_async(session) -> PolarDBClient:
    """Return a cached PolarDBClient, rebuilding when credentials change."""
    global _client, _client_credential_hash

    # Fast path for test overrides set via set_polardb_client()
    if _client is not None and _client_credential_hash == "__test_override__":
        return _client

    from server.config import get_config

    aliyun = get_config().aliyun
    ak = aliyun.access_key_id
    sk = aliyun.access_key_secret
    if ak and sk:
        mode = aliyun.credential_mode
        role_arn = aliyun.role_arn
        session_name = aliyun.role_session_name
        duration = aliyun.sts_duration_seconds
        region = aliyun.region_id
        network = aliyun.openapi_network

        raw = (
            f"{mode}:{ak}:{sk}:{role_arn}:{session_name}:"
            f"{duration}:{region}:{network}"
        )
        h = hashlib.sha256(raw.encode()).hexdigest()

        if _client is not None and h == _client_credential_hash:
            return _client

        from server.aliyun.credential_provider import (
            AssumeRoleProvider,
            CredentialProvider,
            DirectAKProvider,
        )
        from server.aliyun.polardb_client_impl import AliyunPolarDBClient

        provider: CredentialProvider
        if mode == "assume_role":
            provider = AssumeRoleProvider(
                ak=ak, sk=sk, role_arn=role_arn,
                session_name=session_name,
                duration=duration,
                region_id=region,
                openapi_network=network,
            )
        else:
            provider = DirectAKProvider(
                ak=ak,
                sk=sk,
                region_id=region,
                openapi_network=network,
            )

        _client = AliyunPolarDBClient(provider)
        _client_credential_hash = h
        return _client

    # No active cloud-access module: keep the local mock client.
    h = "mock"
    if _client is not None and h == _client_credential_hash:
        return _client

    _client = MockPolarDBClient()
    _client_credential_hash = h
    return _client


def set_polardb_client(client: PolarDBClient) -> None:
    global _client, _client_credential_hash
    _client = client
    _client_credential_hash = "__test_override__"


def reset_polardb_client() -> None:
    global _client, _client_credential_hash
    _client = None
    _client_credential_hash = None
