from __future__ import annotations

import pytest

from server.aliyun.polardb_client import MockPolarDBClient
from server.core.polardb_provisioning_helpers import (
    ProvisioningError,
    generate_db_password,
    resolve_primary_endpoint,
)


def test_generated_db_password_has_required_shape() -> None:
    password = generate_db_password()

    assert len(password) == 19
    assert password[:3] == "Aa1"
    assert password[3:].isalnum()


async def test_resolve_primary_endpoint_prefers_private_primary() -> None:
    client = MockPolarDBClient()
    client.set_endpoint_data("pc-helper", "primary.example.com", 3306)

    assert await resolve_primary_endpoint(client, "pc-helper") == (
        "primary.example.com",
        3306,
    )


async def test_resolve_primary_endpoint_rejects_wrong_network() -> None:
    client = MockPolarDBClient()
    client._endpoints["pc-helper"] = {
        "items": [
            {
                "endpoint_type": "Primary",
                "address_items": [
                    {
                        "connection_string": "public.example.com",
                        "port": "3306",
                        "net_type": "Public",
                    }
                ],
            }
        ]
    }

    with pytest.raises(ProvisioningError, match="no Private endpoint"):
        await resolve_primary_endpoint(client, "pc-helper")
