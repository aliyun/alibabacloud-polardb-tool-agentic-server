import pytest

from server.core.adapter_registry import AdapterNotFound, AdapterRegistry
from server.models import InstanceEngine, InstanceTopology


class FakeAdapter:
    async def create(self, resource):
        del resource

    async def delete(self, resource):
        del resource

    async def verify(self, resource):
        del resource

    async def health_check(self, backend):
        del backend


def test_registry_returns_registered_adapter():
    adapter = FakeAdapter()
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        adapter,
    )

    assert (
        registry.get(
            InstanceEngine.POLARDB_MYSQL,
            InstanceTopology.MULTITENANT,
        )
        is adapter
    )


def test_registry_rejects_unimplemented_engine():
    registry = AdapterRegistry()

    with pytest.raises(AdapterNotFound):
        registry.get("redis", "single_tenant")


def test_registry_rejects_duplicate_registration():
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        FakeAdapter(),
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            InstanceEngine.POLARDB_MYSQL,
            InstanceTopology.MULTITENANT,
            FakeAdapter(),
        )
