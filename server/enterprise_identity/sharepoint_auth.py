from __future__ import annotations

from server.auth.identity_federation import IdentityFederation, UserIdentity
from server.config import OIDCConfig
from server.enterprise_identity.sharepoint import sharepoint_cloud_endpoints


class SharePointAuthenticationError(ValueError):
    pass


def sharepoint_user_login_callback_url(public_base_url: str) -> str:
    normalized_base_url = public_base_url.rstrip("/")
    if not normalized_base_url:
        raise SharePointAuthenticationError("Public base URL must be configured before SharePoint login")
    return f"{normalized_base_url}/auth/sharepoint/login/callback"


def _oidc_config(*, tenant_id: str, client_id: str, client_secret: str, cloud: str) -> OIDCConfig:
    authority_url, _graph_url = sharepoint_cloud_endpoints(cloud)
    authority = f"{authority_url}/{tenant_id}"
    return OIDCConfig(
        issuer=f"{authority}/v2.0",
        authorization_endpoint=f"{authority}/oauth2/v2.0/authorize",
        token_endpoint=f"{authority}/oauth2/v2.0/token",
        jwks_uri=f"{authority}/discovery/v2.0/keys",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["openid", "profile", "email"],
        user_id_claim="oid",
        display_name_claim="name",
        email_claim="preferred_username",
        idp_pkce=True,
        id_token_algorithms=["RS256"],
    )


async def build_sharepoint_authorization_url(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    cloud: str,
    redirect_uri: str,
    state: str,
    nonce: str,
) -> tuple[str, str]:
    federation = IdentityFederation(
        _oidc_config(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            cloud=cloud,
        ),
        provider_name="sharepoint",
    )
    await federation.discover_endpoints()
    authorization_url, code_verifier = federation.build_authorize_url(
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
    )
    if code_verifier is None:
        raise SharePointAuthenticationError("SharePoint login requires PKCE")
    return authorization_url, code_verifier


async def authenticate_sharepoint_user(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    cloud: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    expected_nonce: str,
) -> UserIdentity:
    federation = IdentityFederation(
        _oidc_config(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            cloud=cloud,
        ),
        provider_name="sharepoint",
    )
    token_response = await federation.exchange_code(
        code,
        redirect_uri,
        code_verifier,
    )
    return await federation.extract_user_identity(
        token_response,
        expected_nonce=expected_nonce,
    )
