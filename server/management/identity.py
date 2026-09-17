from __future__ import annotations

from dataclasses import dataclass

from server.configuration.repository import ConfigRepository
from server.management.settings import ManagedIdentitySettings
from server.management.types import ManagedTarget
from server.models import ManagedInstanceBinding


class ManagedIdentityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ManagedIdentityResult:
    status: str
    instance_id: str
    generation: int


class ManagedIdentityBinder:
    def __init__(
        self,
        repository: ConfigRepository,
        environment: ManagedIdentitySettings,
    ) -> None:
        self.repository = repository
        self.environment = environment

    def _verify_environment(self, target: ManagedTarget) -> None:
        if target.instance_id != self.environment.instance_id:
            raise ManagedIdentityError("INSTANCE_IDENTITY_MISMATCH")
        if target.generation != self.environment.generation:
            raise ManagedIdentityError("INSTANCE_GENERATION_MISMATCH")

    @staticmethod
    def _verify_binding(
        binding: ManagedInstanceBinding,
        target: ManagedTarget,
    ) -> None:
        if binding.instance_id != target.instance_id:
            raise ManagedIdentityError("INSTANCE_IDENTITY_MISMATCH")
        if binding.instance_generation != target.generation:
            raise ManagedIdentityError("INSTANCE_GENERATION_MISMATCH")

    @staticmethod
    def _result(
        status: str,
        target: ManagedTarget,
    ) -> ManagedIdentityResult:
        return ManagedIdentityResult(
            status=status,
            instance_id=target.instance_id,
            generation=target.generation,
        )

    async def verify(
        self,
        target: ManagedTarget,
    ) -> ManagedIdentityResult:
        self._verify_environment(target)
        binding = await self.repository.get_managed_identity_binding()
        if binding is None:
            return self._result("UNBOUND", target)
        self._verify_binding(binding, target)
        return self._result("BOUND", target)

    async def bind(
        self,
        target: ManagedTarget,
    ) -> ManagedIdentityResult:
        self._verify_environment(target)
        binding = await self.repository.bind_managed_identity(
            instance_id=target.instance_id,
            generation=target.generation,
        )
        self._verify_binding(binding, target)
        return self._result("BOUND", target)
