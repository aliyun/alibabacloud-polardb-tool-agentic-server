"""AliyunPolarDBClient — real OpenAPI implementation using the Aliyun PolarDB SDK."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from server.aliyun.credential_provider import CredentialProvider
from server.aliyun.diagnostics import (
    error_code_from_error,
    error_detail_from_error,
    request_id_from_error,
)
from server.aliyun.endpoints import resolve_openapi_endpoint
from server.aliyun.polardb_client import OpenAPIError, PolarDBClient
from server.logging import safe_request_id

logger = logging.getLogger(__name__)


class AliyunPolarDBClient(PolarDBClient):
    """PolarDBClient backed by the alibabacloud_polardb20170801 SDK."""

    def __init__(self, credential_provider: CredentialProvider) -> None:
        self._credential_provider = credential_provider
        self._sdk: object | None = None
        self._sdk_endpoint: str | None = None
        self.last_request_id: str | None = None

    async def _get_sdk(self):
        """Return the SDK client backed by the refreshing credentials client."""
        endpoint = resolve_openapi_endpoint(
            "polardb",
            self._credential_provider.region_id,
            self._credential_provider.openapi_network,
        )
        if self._sdk is not None and endpoint == self._sdk_endpoint:
            return self._sdk

        from alibabacloud_polardb20170801.client import Client  # type: ignore[import-untyped]
        from alibabacloud_tea_openapi.models import Config  # type: ignore[import-untyped]

        self._sdk = Client(
            Config(
                credential=self._credential_provider.credential_client,
                region_id=self._credential_provider.region_id,
                endpoint=endpoint,
            )
        )
        self._sdk_endpoint = endpoint
        return self._sdk

    def _wrap_error(
        self, e: Exception, *, operation: str | None = None
    ) -> OpenAPIError:
        return OpenAPIError(
            error_code_from_error(e) or type(e).__name__,
            error_detail_from_error(e)
            or "Alibaba Cloud OpenAPI request failed.",
            request_id=request_id_from_error(e),
            operation=operation,
        )

    @staticmethod
    def _response_request_id(response: object) -> str | None:
        body = getattr(response, "body", None)
        request_id = safe_request_id(getattr(body, "request_id", None))
        if request_id is not None:
            return request_id
        for attribute in ("request_id", "requestId"):
            value = getattr(response, attribute, None)
            request_id = safe_request_id(value)
            if request_id is not None:
                return request_id
        headers = getattr(response, "headers", None)
        if isinstance(headers, Mapping):
            return safe_request_id(headers.get("x-acs-request-id"))
        return None

    async def discover_clusters(self, region_id: str) -> list[dict]:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.DescribeDBClustersRequest(region_id=region_id)
            resp = await sdk.describe_dbclusters_async(req)
            self.last_request_id = self._response_request_id(resp)
            return [
                {"cluster_id": c.dbcluster_id, "status": c.dbcluster_status}
                for c in (resp.body.items.dbcluster or [])
            ]
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="DescribeDBClusters") from e

    async def describe_endpoints(self, cluster_id: str) -> dict:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.DescribeDBClusterEndpointsRequest(dbcluster_id=cluster_id)
            resp = await sdk.describe_dbcluster_endpoints_async(req)
            items = []
            for ep in resp.body.items or []:
                addr_items = []
                for addr in ep.address_items or []:
                    addr_items.append(
                        {
                            "connection_string": addr.connection_string,
                            "port": str(addr.port),
                            "net_type": addr.net_type,
                        }
                    )
                items.append(
                    {
                        "endpoint_type": ep.endpoint_type,
                        "address_items": addr_items,
                    }
                )
            return {"items": items}
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(
                e, operation="DescribeDBClusterEndpoints"
            ) from e

    async def create_account(
        self,
        cluster_id: str,
        account_name: str,
        password: str,
        account_type: str = "Normal",
    ) -> dict:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.CreateAccountRequest(
                dbcluster_id=cluster_id,
                account_name=account_name,
                account_password=password,
                account_type=account_type,
            )
            await sdk.create_account_async(req)
            return {"account_name": account_name}
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="CreateAccount") from e

    async def describe_account(
        self, cluster_id: str, account_name: str
    ) -> dict | None:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.DescribeAccountsRequest(
                dbcluster_id=cluster_id,
                account_name=account_name,
            )
            resp = await sdk.describe_accounts_async(req)
            for account in resp.body.accounts or []:
                if account.account_name == account_name:
                    return {
                        "account_name": account.account_name,
                        "status": account.account_status,
                        "account_type": account.account_type,
                    }
            return None
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="DescribeAccounts") from e

    async def create_agentic_db(self, settings: Mapping[str, str]) -> dict:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.CreateDBClusterRequest(
                region_id=settings.get(
                    "region_id", self._credential_provider.region_id
                ),
                dbtype=settings.get("db_type", "MySQL"),
                dbversion=settings.get("db_version", "8.0"),
                dbminor_version=settings.get("db_minor_version", "8.0.2"),
                dbnode_class=settings.get(
                    "db_node_class", "polar.mysql.sl.small.c"
                ),
                proxy_class=settings.get(
                    "proxy_class", "polar.maxscale.g2.medium.c"
                ),
                proxy_type=settings.get("proxy_type", "GENERAL"),
                architecture=settings.get("architecture", "X86"),
                loose_polar_log_bin=settings.get(
                    "loose_polar_log_bin", "OFF"
                ),
                loose_xengine=settings.get("loose_x_engine", "OFF"),
                pay_type=settings.get("pay_type", "Postpaid"),
                serverless_type=settings.get(
                    "serverless_type", "AgileServerless"
                ),
                scale_min=settings.get("scale_min", "0"),
                scale_max=settings.get("scale_max", "4"),
                allow_shut_down=settings.get("allow_shut_down", "true"),
                scale_ro_num_min=settings.get("scale_ro_num_min", "0"),
                scale_ro_num_max=settings.get("scale_ro_num_max", "1"),
                storage_type=settings.get("storage_type", "essdpl1"),
                storage_space=(
                    int(settings["storage_space"])
                    if settings.get("storage_space")
                    else None
                ),
                vpcid=settings.get("vpc_id") or None,
                v_switch_id=settings.get("vswitch_id") or None,
                zone_id=settings.get("zone_id", ""),
                security_iplist=settings.get("security_ip_list", "127.0.0.1"),
                agentic_db_type="dedicated",
            )
            resp = await sdk.create_dbcluster_async(req)
            result = {"cluster_id": resp.body.dbcluster_id}
            agentic_id = getattr(resp.body, "agentic_db_cluster_id", None)
            if agentic_id:
                result["agentic_db_cluster_id"] = agentic_id
            return result
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="CreateDBCluster") from e

    async def create_dedicated_cluster(
        self,
        params: Mapping[str, str],
        agentic_db_type: str,
        agentic_db_cluster_id: str | None,
        agentic_db_cluster_description: str,
        db_cluster_description: str,
    ) -> dict:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.CreateDBClusterRequest(
                region_id=params.get("region_id"),
                dbtype=params.get("db_type"),
                dbversion=params.get("db_version"),
                dbminor_version=params.get("db_minor_version"),
                dbnode_class=params.get("db_node_class"),
                proxy_class=params.get("proxy_class"),
                proxy_type=params.get("proxy_type"),
                architecture=params.get("architecture"),
                loose_polar_log_bin=params.get("loose_polar_log_bin"),
                loose_xengine=params.get("loose_x_engine"),
                pay_type=params.get("pay_type"),
                serverless_type=params.get("serverless_type"),
                scale_min=params.get("scale_min"),
                scale_max=params.get("scale_max"),
                allow_shut_down=params.get("allow_shut_down"),
                scale_ro_num_min=params.get("scale_ro_num_min"),
                scale_ro_num_max=params.get("scale_ro_num_max"),
                storage_type=params.get("storage_type"),
                storage_space=int(params["storage_space"]) if params.get("storage_space") else None,
                vpcid=params.get("vpc_id") or None,
                v_switch_id=params.get("vswitch_id") or None,
                zone_id=params.get("zone_id"),
                security_iplist=params.get("security_ip_list"),
                client_token=params.get("client_token"),
                agentic_db_type=agentic_db_type,
                agentic_db_cluster_id=agentic_db_cluster_id,
                agentic_db_cluster_description=agentic_db_cluster_description,
                dbcluster_description=db_cluster_description,
            )
            resp = await sdk.create_dbcluster_async(req)
            self.last_request_id = self._response_request_id(resp)
            return {
                "cluster_id": resp.body.dbcluster_id,
                "agentic_db_cluster_id": resp.body.agentic_db_cluster_id,
                "agentic_db_cluster_description": resp.body.agentic_db_cluster_description,
                "request_id": self.last_request_id,
            }
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="CreateDBCluster") from e

    async def delete_cluster(self, cluster_id: str) -> None:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.DeleteDBClusterRequest(dbcluster_id=cluster_id)
            await sdk.delete_dbcluster_async(req)
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="DeleteDBCluster") from e

    async def describe_cluster_attribute(self, cluster_id: str) -> dict:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            req = m.DescribeDBClusterAttributeRequest(dbcluster_id=cluster_id)
            resp = await sdk.describe_dbcluster_attribute_async(req)
            return {"status": resp.body.dbcluster_status}
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(
                e, operation="DescribeDBClusterAttribute"
            ) from e

    async def create_database(
        self,
        cluster_id: str,
        db_name: str,
        account_name: str | None = None,
        character_set: str = "utf8",
        account_privilege: str = "ReadWrite",
    ) -> None:
        from alibabacloud_polardb20170801 import models as m  # type: ignore[import-untyped]

        sdk = await self._get_sdk()
        try:
            request_values = dict(
                dbcluster_id=cluster_id,
                dbname=db_name,
                character_set_name=character_set,
            )
            if account_name:
                request_values.update(
                    account_name=account_name,
                    account_privilege=account_privilege,
                )
            req = m.CreateDatabaseRequest(**request_values)
            await sdk.create_database_async(req)
        except OpenAPIError:
            raise
        except Exception as e:
            raise self._wrap_error(e, operation="CreateDatabase") from e
