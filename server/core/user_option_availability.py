"""Candidate eligibility only; never changes grants or account/token state."""

from sqlalchemy import and_, case, exists, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.enterprise_identity.service import identity_provider_key, identity_source_snapshot_is_usable
from server.models import (
    AgentPolarRAGInstanceBinding, AuthProvider, EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryUser, EnterpriseIdentitySource, EnterpriseIdentitySourceSpaceBinding,
    ExternalUserPrincipalMembership, KnowledgeResource, PolarRAGSpace, User, UserExternalIdentity, UserStatus,
)
from server.models.base import utc_now


async def user_option_availability(
    session: AsyncSession, agent_id: str, *, resource_ids: list[str] | None = None,
):
    spaces = select(PolarRAGSpace.knowledge_space_id).join(
        AgentPolarRAGInstanceBinding,
        AgentPolarRAGInstanceBinding.polarrag_instance_id == PolarRAGSpace.polarrag_instance_id,
    ).where(AgentPolarRAGInstanceBinding.agent_id == agent_id, PolarRAGSpace.enabled.is_(True))
    if resource_ids is not None:
        spaces = spaces.where(PolarRAGSpace.knowledge_space_id.in_(
            select(KnowledgeResource.knowledge_space_id).where(KnowledgeResource.id.in_(resource_ids)),
        ))
    sources = list(await session.scalars(select(EnterpriseIdentitySource).where(
        EnterpriseIdentitySource.id.in_(select(EnterpriseIdentitySourceSpaceBinding.identity_source_id).where(
            EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(spaces),
        )),
    )))
    source_match = or_(literal(False), *[
        and_(EnterpriseDirectoryUser.identity_source_id == source.id,
             UserExternalIdentity.identity_provider == identity_provider_key(source))
        for source in sources
    ])
    disabled_identity = exists(select(EnterpriseDirectoryUser.id).join(
        UserExternalIdentity,
        UserExternalIdentity.external_subject == EnterpriseDirectoryUser.external_user_id,
    ).where(UserExternalIdentity.user_id == User.id, source_match,
            EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.DISABLED))
    valid_identities = []
    for source in sources:
        if not identity_source_snapshot_is_usable(source):
            continue
        directory = select(EnterpriseDirectoryUser.id).where(
            EnterpriseDirectoryUser.identity_source_id == source.id,
            EnterpriseDirectoryUser.external_user_id == UserExternalIdentity.external_subject,
        )
        local_membership = exists(select(ExternalUserPrincipalMembership.id).where(
            ExternalUserPrincipalMembership.identity_source_id == source.id,
            ExternalUserPrincipalMembership.external_user_id == UserExternalIdentity.external_subject,
            or_(ExternalUserPrincipalMembership.expires_at.is_(None),
                ExternalUserPrincipalMembership.expires_at > utc_now()),
        ))
        valid_identities.append(exists(select(UserExternalIdentity.id).where(
            UserExternalIdentity.user_id == User.id,
            UserExternalIdentity.identity_provider == identity_provider_key(source),
            or_(directory.where(EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE).exists(),
                and_(source.provider.value != "dingtalk", ~directory.exists(), local_membership)),
        )))
    enterprise_identity = exists(select(UserExternalIdentity.id).where(
        UserExternalIdentity.user_id == User.id,
        or_(UserExternalIdentity.identity_provider.startswith("feishu:"),
            UserExternalIdentity.identity_provider.startswith("sharepoint:"),
            UserExternalIdentity.identity_provider.startswith("dingtalk:")),
    ))
    # Built-in credentials and ordinary OIDC accounts do not require a directory.
    usable = and_(User.status == UserStatus.ACTIVE, or_(
        User.auth_provider == AuthProvider.BUILTIN, ~enterprise_identity, *valid_identities,
    ))
    return case(
        (usable, literal("active")),
        (or_(User.status == UserStatus.DISABLED, disabled_identity), literal("disabled")),
        else_=literal("unavailable"),
    )
