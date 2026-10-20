from __future__ import annotations

import json
import logging

from cryptography.exceptions import InvalidTag

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import decrypt
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    PolarRAGSpace,
    IdentitySourceProvider,
)

logger = logging.getLogger(__name__)


class IdentityDomainTenantConflict(Exception):
    """Two DingTalk corporations cannot share one identity domain."""


def identity_source_binds_all_spaces(source: EnterpriseIdentitySource) -> bool:
    if not source.config_ciphertext:
        return True
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except (TypeError, ValueError, InvalidTag):
        logger.warning("Invalid identity source configuration; automatic Space binding disabled")
        return False
    if not isinstance(config, dict) or not isinstance(config.get("bind_all_spaces", True), bool):
        logger.warning("Invalid identity source binding policy; automatic Space binding disabled")
        return False
    return config.get("bind_all_spaces", True) is True


async def bind_identity_source_to_enabled_spaces(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
) -> list[str]:
    space_ids = list(
        (
            await session.execute(select(PolarRAGSpace.knowledge_space_id).where(PolarRAGSpace.enabled.is_(True)))
        ).scalars()
    )
    for knowledge_space_id in space_ids:
        await ensure_identity_domain_tenant(session, source, await session.get(PolarRAGSpace, knowledge_space_id))
        session.add(
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=source.id,
                knowledge_space_id=knowledge_space_id,
            )
        )
    if space_ids:
        await session.flush()
    return space_ids


async def bind_default_identity_sources_to_space(
    session: AsyncSession,
    space: PolarRAGSpace,
) -> None:
    sources = list((await session.execute(select(EnterpriseIdentitySource))).scalars())
    for source in sources:
        if identity_source_binds_all_spaces(source):
            await ensure_identity_domain_tenant(session, source, space)
            session.add(
                EnterpriseIdentitySourceSpaceBinding(
                    identity_source_id=source.id,
                    knowledge_space_id=space.knowledge_space_id,
                )
            )
    if sources:
        await session.flush()


async def ensure_identity_domain_tenant(session, source, space):
    """Serialize one identity_domain's DingTalk bindings; raw userIds are corp-scoped."""
    if source.provider != IdentitySourceProvider.DINGTALK:
        return
    domain_spaces = list(await session.scalars(select(PolarRAGSpace).where(
        PolarRAGSpace.identity_domain == space.identity_domain).order_by(PolarRAGSpace.knowledge_space_id).with_for_update()))
    if domain_spaces:
        # SQLite also serializes concurrent writers on the same domain.
        await session.execute(update(PolarRAGSpace).where(PolarRAGSpace.knowledge_space_id == domain_spaces[0].knowledge_space_id)
                              .values(identity_domain=space.identity_domain, updated_at=PolarRAGSpace.updated_at))
    tenants = set(await session.scalars(select(EnterpriseIdentitySource.tenant_id)
        .join(EnterpriseIdentitySourceSpaceBinding, EnterpriseIdentitySourceSpaceBinding.identity_source_id == EnterpriseIdentitySource.id)
        .join(PolarRAGSpace, PolarRAGSpace.knowledge_space_id == EnterpriseIdentitySourceSpaceBinding.knowledge_space_id)
        .where(PolarRAGSpace.identity_domain == space.identity_domain,
               EnterpriseIdentitySource.provider == IdentitySourceProvider.DINGTALK,
               EnterpriseIdentitySource.id != source.id)))
    if tenants - {source.tenant_id}:
        raise IdentityDomainTenantConflict("DINGTALK_IDENTITY_DOMAIN_TENANT_CONFLICT")
