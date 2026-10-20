from datetime import UTC, datetime, timedelta
import pytest
from sqlalchemy import select
from pydantic import ValidationError
from server.core.user_option_availability import user_option_availability
from server.api.polarrag import PrincipalCreate
from server.models import (AgentGroupAssignment, EnterpriseIdentitySource, IdentitySourceProvider, User, UserExternalIdentity, AuthProvider)
from tests.test_knowledge_visibility_bind import catalog as catalog

pytest_plugins = ("tests._admin_api_fixtures",)

async def test_expired_dingtalk_candidate_is_unavailable(setup, catalog):
    async with setup[0]() as session:
        source = await session.scalar(select(EnterpriseIdentitySource))
        source.provider = IdentitySourceProvider.DINGTALK
        source.status = "stale"
        source.last_synced_at = datetime.now(UTC) - timedelta(days=1)
        user = User(external_id="review-dingtalk", display_name="Review", auth_provider=AuthProvider.OIDC)
        session.add(user)
        await session.flush()
        mapping = UserExternalIdentity(user_id=user.id, identity_provider="dingtalk:visibility", external_subject="u1")
        session.add(mapping)
        await session.flush()
        availability = await user_option_availability(session, catalog[0])
        assert await session.scalar(select(availability).select_from(User).where(User.id == user.id)) == "unavailable"
        mapping.identity_provider = "feishu:visibility"
        await session.flush()
        availability = await user_option_availability(session, catalog[0])
        assert await session.scalar(select(availability).select_from(User).where(User.id == user.id)) == "unavailable"

async def test_unexpanded_dingtalk_group_rejected_before_database(setup, catalog):
    async with setup[0]():
        with pytest.raises(ValueError, match="not supported"):
            AgentGroupAssignment.for_enterprise_group(agent_id=catalog[0], identity_domain="domain", provider="dingtalk", principal_id="g1", created_by_user_id=setup[1].id)

def test_manual_dingtalk_user_supported_but_conversation_rejected():
    assert PrincipalCreate(identity_domain="domain", provider="dingtalk", principal_type="user", principal_id=" u1 ").principal_id == "u1"
    with pytest.raises(ValidationError, match="not supported"):
        PrincipalCreate(identity_domain="domain", provider="dingtalk", principal_type="group", principal_id="cid1")
