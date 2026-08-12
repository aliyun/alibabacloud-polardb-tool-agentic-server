from __future__ import annotations

import secrets
import string

from server.aliyun.polardb_client import PolarDBClient


class ProvisioningError(Exception):
    pass


def generate_db_password() -> str:
    """Generate a password that satisfies PolarDB MySQL complexity rules."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"Aa1{body}"


async def resolve_primary_endpoint(
    client: PolarDBClient,
    cluster_id: str,
    preferred_net_type: str = "Private",
) -> tuple[str, int]:
    """Resolve the best endpoint with the requested network type."""
    result = await client.describe_endpoints(cluster_id)
    items = result.get("items", [])
    by_type = {item["endpoint_type"]: item for item in items}
    chosen = (
        by_type.get("Primary")
        or by_type.get("PrimaryOnProxy")
        or by_type.get("Cluster")
    )
    if not chosen or not chosen.get("address_items"):
        raise ProvisioningError(
            f"no usable endpoint for {cluster_id}; "
            f"available types: {list(by_type.keys())}"
        )
    address = next(
        (
            item
            for item in chosen["address_items"]
            if item.get("net_type") == preferred_net_type
        ),
        None,
    )
    if address is None:
        available_net_types = sorted(
            {
                str(item.get("net_type"))
                for item in chosen["address_items"]
                if item.get("net_type")
            }
        )
        raise ProvisioningError(
            f"no {preferred_net_type} endpoint for {cluster_id}; "
            f"available network types: {available_net_types}"
        )
    return address["connection_string"], int(address["port"])
