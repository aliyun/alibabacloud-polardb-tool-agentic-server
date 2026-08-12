from __future__ import annotations

from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from alibabacloud_polardb20170801 import models as polardb_models
from server.aliyun.credential_provider import DirectAKProvider
from server.aliyun.polardb_client import OpenAPIError
from server.aliyun.polardb_client_impl import AliyunPolarDBClient


async def test_sdk_uses_vpc_endpoint_for_vpc_network() -> None:
    provider = DirectAKProvider(
        ak="ak",
        sk="sk",
        region_id="cn-beijing",
        openapi_network="vpc",
    )
    client = AliyunPolarDBClient(provider)

    with patch(
        "alibabacloud_polardb20170801.client.Client"
    ) as client_type:
        await client._get_sdk()

    sdk_config = client_type.call_args.args[0]
    assert sdk_config.endpoint == "polardb-vpc.cn-beijing.aliyuncs.com"


async def test_sdk_receives_dynamic_credentials_client_without_static_keys() -> None:
    provider = DirectAKProvider(
        access_key_id="ak",
        access_key_secret="sk",
        region_id="cn-hangzhou",
    )
    client = AliyunPolarDBClient(provider)

    with patch(
        "alibabacloud_polardb20170801.client.Client"
    ) as client_type:
        await client._get_sdk()

    sdk_config = client_type.call_args.args[0]
    assert sdk_config.credential is provider.credential_client
    assert sdk_config.access_key_id is None
    assert sdk_config.access_key_secret is None
    assert sdk_config.security_token is None


def test_sdk_error_wrapper_keeps_only_code_and_safe_request_id() -> None:
    class SDKError(Exception):
        code = "Forbidden.RAM"
        request_id = "polardb-request:123"

        def __str__(self) -> str:
            return "access_key_secret=secret response body"

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    wrapped = client._wrap_error(SDKError())

    assert isinstance(wrapped, OpenAPIError)
    assert wrapped.code == "Forbidden.RAM"
    assert wrapped.request_id == "polardb-request:123"
    assert "secret response body" not in str(wrapped)


def test_sdk_error_wrapper_keeps_bounded_structured_cloud_detail() -> None:
    class SDKError(Exception):
        code = "InvalidAccountName.NotFound"
        request_id = "request-account-missing"
        message = "Account pas_lifecycle_demo is not exist!"

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    wrapped = client._wrap_error(SDKError(), operation="CreateDatabase")

    assert wrapped.message == "Account pas_lifecycle_demo is not exist!"
    assert wrapped.operation == "CreateDatabase"


def test_sdk_error_wrapper_rejects_sensitive_structured_cloud_detail() -> None:
    class SDKError(Exception):
        code = "Forbidden.RAM"
        message = "access_key_secret=must-not-leak"

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    wrapped = client._wrap_error(SDKError(), operation="CreateAccount")

    assert wrapped.message == "Alibaba Cloud OpenAPI request failed."
    assert "must-not-leak" not in str(wrapped)


async def test_discover_clusters_reads_request_id_from_generated_response_body() -> None:
    class SDK:
        async def describe_dbclusters_async(self, request):
            del request
            return polardb_models.DescribeDBClustersResponse(
                body=polardb_models.DescribeDBClustersResponseBody(
                    request_id="body-request:456",
                    items=polardb_models.DescribeDBClustersResponseBodyItems(
                        dbcluster=[]
                    ),
                )
            )

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    client._sdk = SDK()
    client._sdk_endpoint = "polardb.aliyuncs.com"

    await client.discover_clusters("cn-hangzhou")

    assert client.last_request_id == "body-request:456"


async def test_create_account_defaults_normal_and_allows_lifecycle_super() -> None:
    requests = []

    class SDK:
        async def create_account_async(self, request):
            requests.append(request)

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    client._get_sdk = AsyncMock(return_value=SDK())  # type: ignore[method-assign]

    await client.create_account("pc-default", "agentic", "password")
    await client.create_account(
        "pc-lifecycle",
        "pas_lifecycle",
        "password",
        account_type="Super",
    )

    assert [request.account_type for request in requests] == [
        "Normal",
        "Super",
    ]


async def test_describe_account_returns_status_only_for_matching_account() -> None:
    requests = []

    class SDK:
        async def describe_accounts_async(self, request):
            requests.append(request)
            return SimpleNamespace(
                body=SimpleNamespace(
                    accounts=[
                        SimpleNamespace(
                            account_name="agentic",
                            account_status="Available",
                            account_type="Normal",
                        )
                    ]
                )
            )

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    client._get_sdk = AsyncMock(return_value=SDK())  # type: ignore[method-assign]

    account = await client.describe_account("pc-test", "agentic")

    assert requests[0].dbcluster_id == "pc-test"
    assert requests[0].account_name == "agentic"
    assert account == {
        "account_name": "agentic",
        "status": "Available",
        "account_type": "Normal",
    }


async def test_create_database_omits_account_assignment() -> None:
    requests = []

    class SDK:
        async def create_database_async(self, request):
            requests.append(request)

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    client._get_sdk = AsyncMock(return_value=SDK())  # type: ignore[method-assign]

    await client.create_database(
        "pc-test",
        "agentic",
        character_set="utf8",
    )

    request = requests[0]
    assert request.dbcluster_id == "pc-test"
    assert request.dbname == "agentic"
    assert request.account_name is None
    assert request.character_set_name == "utf8"
    assert request.account_privilege is None


async def test_dedicated_purchase_forwards_stable_token_and_request_id() -> None:
    requests = []

    class SDK:
        async def create_dbcluster_async(self, request):
            requests.append(request)
            return SimpleNamespace(
                body=SimpleNamespace(
                    dbcluster_id="pc-created",
                    agentic_db_cluster_id="pagc-created",
                    agentic_db_cluster_description="pool member",
                    request_id="request-created",
                )
            )

    client = AliyunPolarDBClient(
        DirectAKProvider(access_key_id="ak", access_key_secret="sk")
    )
    client._get_sdk = AsyncMock(return_value=SDK())  # type: ignore[method-assign]

    result = await client.create_dedicated_cluster(
        {"region_id": "cn-hangzhou", "client_token": "stable-token"},
        "dedicated",
        None,
        "pool member",
        "cluster member",
    )

    assert requests[0].client_token == "stable-token"
    assert result["request_id"] == "request-created"
    assert client.last_request_id == "request-created"
