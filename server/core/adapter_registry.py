from __future__ import annotations

from server.core.provisioning_adapter import ProvisioningAdapter
from server.models import InstanceEngine, InstanceTopology


class AdapterNotFound(LookupError):
    pass


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], ProvisioningAdapter] = {}

    @staticmethod
    def _key(engine: InstanceEngine | str, topology: InstanceTopology | str) -> tuple[str, str]:
        engine_value = engine.value if isinstance(engine, InstanceEngine) else engine
        topology_value = (
            topology.value if isinstance(topology, InstanceTopology) else topology
        )
        return engine_value, topology_value

    def register(
        self,
        engine: InstanceEngine | str,
        topology: InstanceTopology | str,
        adapter: ProvisioningAdapter,
    ) -> None:
        key = self._key(engine, topology)
        if key in self._adapters:
            raise ValueError(f"Adapter already registered for {key[0]}/{key[1]}")
        self._adapters[key] = adapter

    def get(
        self,
        engine: InstanceEngine | str,
        topology: InstanceTopology | str,
    ) -> ProvisioningAdapter:
        key = self._key(engine, topology)
        try:
            return self._adapters[key]
        except KeyError as error:
            raise AdapterNotFound(
                f"No provisioning adapter for {key[0]}/{key[1]}"
            ) from error
