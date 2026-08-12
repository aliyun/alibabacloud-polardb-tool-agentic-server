from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.configuration.types import ModuleDocument
from server.models import (
    AllocationMode,
    DedicatedPoolMember,
    Instance,
    QuotaCounter,
    SystemConfig,
)


@dataclass(frozen=True, slots=True)
class LegacyPoolAudit:
    legacy_target_size: int
    legacy_config_invalid: bool
    unlinked_physical_instances: int
    member_mode_mismatches: int
    user_owned_instances: int
    placeholder_instances: int
    quota_held_instances: int
    nonzero_quota_counters: int

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        codes: list[str] = []
        if self.legacy_target_size > 0 or self.legacy_config_invalid:
            codes.append("LEGACY_POOL_CONFIG_PRESENT")
        if self.unlinked_physical_instances or self.member_mode_mismatches:
            codes.append("LEGACY_POOL_INSTANCE_PRESENT")
        return tuple(codes)


class LegacyPoolStatePresent(RuntimeError):
    def __init__(self, code: str, audit: LegacyPoolAudit) -> None:
        super().__init__(code)
        self.code = code
        self.audit = audit


def _legacy_target(document_value: str) -> tuple[int, bool]:
    try:
        document = ModuleDocument.model_validate_json(document_value)
    except ValueError:
        return 0, True
    if document.effective is None:
        return 0, False
    value = document.effective.config.get("target_size", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0, True
    return value, False


async def audit_legacy_pool_state(session: AsyncSession) -> LegacyPoolAudit:
    config_row = await session.get(SystemConfig, "module.resource_pool")
    if config_row is None:
        legacy_target_size, legacy_config_invalid = 0, False
    else:
        legacy_target_size, legacy_config_invalid = _legacy_target(
            config_row.config_value
        )

    member_instance_ids = select(DedicatedPoolMember.instance_id)
    legacy_modes = (
        AllocationMode.POOLED,
        AllocationMode.AUTO_PROVISIONED,
    )
    unlinked_physical_instances = int(
        await session.scalar(
            select(func.count(Instance.id)).where(
                Instance.allocation_mode.in_(legacy_modes),
                ~Instance.id.in_(member_instance_ids),
            )
        )
        or 0
    )
    member_mode_mismatches = int(
        await session.scalar(
            select(func.count(DedicatedPoolMember.id))
            .join(Instance, Instance.id == DedicatedPoolMember.instance_id)
            .where(Instance.allocation_mode != AllocationMode.DEDICATED_POOL)
        )
        or 0
    )
    user_owned_instances = int(
        await session.scalar(
            select(func.count(Instance.id)).where(
                Instance.allocation_mode.in_(legacy_modes),
                Instance.owner_user_id.is_not(None),
            )
        )
        or 0
    )
    placeholder_instances = int(
        await session.scalar(
            select(func.count(Instance.id)).where(
                Instance.allocation_mode.in_(legacy_modes),
                (
                    Instance.cluster_id.startswith("pending-")
                    | Instance.cluster_id.startswith("pool-pending-")
                ),
            )
        )
        or 0
    )
    quota_held_instances = int(
        await session.scalar(
            select(func.count(Instance.id)).where(Instance.quota_held.is_(True))
        )
        or 0
    )
    nonzero_quota_counters = int(
        await session.scalar(
            select(func.count(QuotaCounter.scope)).where(
                QuotaCounter.current_count > 0
            )
        )
        or 0
    )
    return LegacyPoolAudit(
        legacy_target_size=legacy_target_size,
        legacy_config_invalid=legacy_config_invalid,
        unlinked_physical_instances=unlinked_physical_instances,
        member_mode_mismatches=member_mode_mismatches,
        user_owned_instances=user_owned_instances,
        placeholder_instances=placeholder_instances,
        quota_held_instances=quota_held_instances,
        nonzero_quota_counters=nonzero_quota_counters,
    )


async def assert_legacy_purchase_safe(session: AsyncSession) -> None:
    audit = await audit_legacy_pool_state(session)
    if audit.blocking_codes:
        raise LegacyPoolStatePresent(audit.blocking_codes[0], audit)
