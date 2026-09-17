# Authentication

[简体中文](../../zh-cn/administration/authentication.md)

PAS separates bootstrap ownership, human authentication, and Agent Tokens.
Credentials from one identity type cannot be substituted for another.

## Bootstrap and built-in login

The one-time bootstrap token is accepted only while the system is in setup
mode and is consumed when `core_admin` activates. The first administrator then
uses the built-in login. Session cookies require the CSRF header used by the
Web console; API clients should use the supported bearer flow.

The retained `set_initial_password` command is first-only: it initializes the
built-in administrator from `RESET_REQUIRED` to `ACTIVE`, and later attempts
return `ADMIN_PASSWORD_ALREADY_INITIALIZED`. After initialization, the isolated,
opt-in management listener supports password `MODIFY` (with the current
password) and password `RESET` (without an old password) for the fixed `admin`
account. `RESET` is valid in both `RESET_REQUIRED` and `ACTIVE`.

Every successful built-in password initialization, modification, or reset
increments the credential epoch, revokes REST and MCP OAuth refresh tokens,
invalidates outstanding built-in authorization codes, and rejects existing
password-authenticated access sessions. No endpoint returns a password. Never
place bootstrap tokens or passwords in a URL or configuration repository.

## Web console language

The Web console supports English (`en-US`) and Simplified Chinese (`zh-CN`).
On the first visit it follows the browser language and falls back to English
when the language is unsupported. Use the language switcher on the login,
setup, or authenticated console screen to override that choice. An explicit
choice is stored in the browser and takes precedence on later visits.

Changing the display language affects only frontend labels, messages, Ant
Design components, and locale-aware date or number formatting. API payloads,
identifiers, SQL, and backend diagnostic details are not translated.

## Optional SSO

The `user_sso` module can remain `SKIPPED`. When it is active, the ordinary
console login page shows only the configured enterprise SSO action. The
separate `/login/recovery` route retains built-in access for an active
administrator. Recovery login is rate-limited and audited; it is not a second
login option for ordinary users.

Configure `runtime_policy.external_base_url` with the externally reachable
HTTPS origin first. The SSO form displays the only callback URL that must be
registered at the identity provider:

```text
https://PAS_HOST/auth/oidc/callback
```

For same-machine development without a public IdP, start PAS with
`pas serve --local-sso-dev`. In that explicit mode only, the external base URL
may use HTTP only on `localhost` or `127.0.0.1`, matching the IPv4 listener.
Provider endpoints may additionally use HTTP on `::1`. Every PAS listener is
bound to `127.0.0.1`; remote, LAN, wildcard, other `127/8`, and lookalike
destinations remain rejected.

Use OIDC discovery when possible. Manual mode requires `issuer`,
`authorization_endpoint`, `token_endpoint`, and `jwks_uri`; UserInfo is
optional. Saving a draft is not enough to activate SSO. An administrator must:

1. save and validate the current revision;
2. complete the browser login test as that same administrator;
3. activate the same revision with the successful test proof.

PAS binds the proof to the administrator, revision, normalized configuration
digest, identity-provider subject, and expiry. Editing the draft invalidates
the proof. Activation also prevents an identity already owned by another PAS
user from being silently rebound. Leaving an already configured client secret
blank preserves the encrypted value.

Validation fetches discovery and JWKS documents from the PAS backend with
timeouts, a response-size limit, redirects disabled, HTTPS enforcement, and
public-address checks. The explicit localhost development mode substitutes
strict loopback resolution checks for its permitted HTTP endpoints. Validation
verifies issuer consistency and required endpoint shape without returning the
client secret or raw provider response.

For both console login and MCP authorization, PAS binds each OIDC callback to
a short-lived, one-time state and OIDC nonce. Optional IdP-side PKCE uses
S256. PAS verifies the ID Token signature, issuer, audience, and nonce. When
PAS also reads UserInfo, its `sub` must exactly match the verified ID Token
`sub`. A successful callback maps or JIT-creates a PAS user; PAS then issues
its own browser session or OAuth grant. In this browser flow, the provider
token is not the MCP credential and PAS does not forward every MCP request to
the provider. Separately, an administrator may enable trusted external access
tokens for Token Exchange or the opt-in direct MCP compatibility mode described
below. Validation failures return a generic, non-cacheable authentication error
and consume the one-time state.

## MCP OAuth security contract

PAS supports OAuth 2.0 Authorization Code and Refresh Token grants with an
OAuth 2.1 security profile. PKCE with `S256` is mandatory between the MCP
client and PAS; implicit and resource-owner-password grants are not exposed.
OAuth-capable MCP clients may use dynamic client registration or a
pre-registered client. Public clients use `none`; confidential clients may use
`client_secret_basic` or `client_secret_post`.

Every redirect URI is stored and compared exactly. Remote callbacks must use
HTTPS. HTTP is accepted only for loopback clients using `localhost`,
`127.0.0.1`, or `::1`; a loopback registration may match the client's
ephemeral callback port. Credentials, fragments, wildcards, custom schemes,
and unregistered redirects are rejected.

PAS publishes Protected Resource and Authorization Server metadata. The client
discovers the authorization server from `/mcp`, opens the system browser,
completes SSO, exchanges the authorization code, and refreshes the PAS token.
The final MCP request still uses
`Authorization: Bearer <PAS access token>`; the difference is that the client,
not the user, obtains and refreshes that credential. The OAuth `resource`
value and access-token `aud` identify the PAS `/mcp` endpoint.

An MCP access JWT is signed by PAS and requires `iss`, `aud`, `sub`, `iat`,
`exp`, `jti`, and `type`. `iss` is the exact PAS external base URL and `aud` is
the `/mcp` resource URL. Signature, issuer, audience, expiry, subject, unique
token ID, and `type=access` are checked on every request. The issuer is internal
to the JWT; it is not an extra field in copied MCP JSON.

PAS applies pod-local limits before credential or grant validation: five
builtin login attempts per account per minute, 20 builtin
login attempts per remote address per minute, 10 dynamic registrations per
remote address per minute, and 20 token requests per remote address per minute.
An exceeded limit returns HTTP `429` and `Retry-After`. These minimum limits do
not provide a distributed cluster-wide quota; enforce additional protection at
the Ingress when running multiple replicas.

## Trusted external access tokens

The active `user_sso` module may trust one external access-token provider.
Supported validation adapters are:

- OIDC JWT access tokens with `at+jwt` typing, signature, issuer, audience,
  expiry, subject, client ID, issued-at time, and token ID validation;
- OAuth 2.0 Token Introspection, requiring an `active` response and optionally
  an expected audience;
- OAuth 2.0 UserInfo, with the access token in an Authorization header, form
  body, or query parameter as required by the provider;
- Feishu User Access Tokens, verified against the active, verified Feishu
  enterprise identity source identified by each Token Exchange request;
- BUC Access Tokens, using form-post UserInfo and checking `client_id` and
  `account_id`.

The preferred protocol is standard OAuth Token Exchange. Administrators should
prepare an integration in one place:

1. Under **Service Configuration > User SSO**, turn off **Enable browser SSO
   login** when only Token Exchange is needed, then configure, validate, and
   activate the external access-token Provider. No callback URL or browser
   endpoints and credentials are required in this mode.
2. Open **External Applications** and register the calling server.
3. Select the allowed target (`MCP`, `API`, or both) and an Agent policy.
4. Store the displayed `client_id` and one-time `client_secret` in the
   calling server's secret manager.
5. Copy the displayed endpoint, resource, scope, and request example. The
   application detail page remains the source of truth for these values.

PAS creates a confidential OAuth client for each administrator-managed
application and derives the allowed resource profiles from
`runtime_policy.external_base_url`:

| Target | Resource | Scope |
| --- | --- | --- |
| MCP | `<PAS base URL>/mcp` | `mcp` |
| HTTP API | `<PAS base URL>/api/v1` | `polarrag` |

An application with one target may omit `resource`; PAS selects its only
profile. An application with both targets must send `resource`. The caller may
omit `scope`, because PAS derives it from the selected resource. If supplied,
it must exactly match the profile; callers cannot request arbitrary scopes.

The standard endpoint is `/token`; `/api/v1/external-auth/token` is an
equivalent compatibility alias:

```http
POST /token
Authorization: Basic base64(PAS_CLIENT_ID:PAS_CLIENT_SECRET)
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange&
subject_token=EXTERNAL_ACCESS_TOKEN&
subject_token_type=urn:ietf:params:oauth:token-type:access_token&
resource=https%3A%2F%2FPAS_HOST%2Fmcp&
scope=mcp
```

When the configured external Token Provider is **Feishu User Access Token**,
`subject_token` is the user's Feishu `user_access_token`. Each exchange must
also include these PAS extension parameters exactly once:

```text
identity_source_id=PAS_VERIFIED_FEISHU_SOURCE_ID&
feishu_user_id=FEISHU_USER_ID&
feishu_union_id=FEISHU_UNION_ID
```

PAS calls Feishu UserInfo with `subject_token`, then requires its `tenant_key`,
`user_id`, and `union_id` to match the supplied identity source and values. An
expired Feishu token, an inactive or foreign source, or any mismatch returns
`invalid_grant`. PAS does not persist the Feishu token or map a PAS token back
to it; the returned PAS token follows the configured PAS lifetime.

PAS returns its own access token, does not issue a refresh token, and returns
`issued_token_type=urn:ietf:params:oauth:token-type:access_token`. Omitting
`grant_type` from `/token` never triggers an inferred exchange.

The selected Agent policy controls `agent_id`:

| Policy | Request behavior |
| --- | --- |
| Workspace default | Do not send `agent_id`; PAS uses the user's workspace default. |
| Fixed Agent | PAS always uses the configured Agent; a supplied value must match it. |
| Caller selectable | The caller may select an authorized Agent; omission uses the workspace default. |

The application page can test a real external subject token before credentials
are handed to an integration team. For a Feishu provider, the test dialog also
requires the PAS identity-source UUID, Feishu user ID, and Feishu union ID and
sends them as `identity_source_id`, `feishu_user_id`, and `feishu_union_id`.
Rotating a secret immediately invalidates the old secret. Disabling an
application rejects new exchanges. The `client_secret` is displayed only when
the application is created or its secret is rotated.

Dynamic client registration remains available for compatible clients. A DCR
client that declares Token Exchange must register at least one scope:

```http
POST /register
Content-Type: application/json

{
  "token_endpoint_auth_method": "client_secret_basic",
  "grant_types": [
    "urn:ietf:params:oauth:grant-type:token-exchange"
  ],
  "scope": "mcp polarrag",
  "client_name": "External service"
}
```

Public DCR clients authenticate with `none`; confidential DCR clients use
`client_secret_basic` or `client_secret_post` according to their registration.
Token Exchange-only DCR clients may omit `redirect_uris` and
`response_types`. The administrator-managed **External Applications** flow is
recommended for server-to-server integrations because it also controls
targets, Agent policy, lifecycle, and copy-ready integration parameters.

Token Exchange failures use the OAuth error response
`{"error":"...","error_description":"..."}`:

| HTTP | Error | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | Required or single-valued form data is invalid. |
| `400` | `unauthorized_client` | The PAS OAuth client did not register the Token Exchange grant. |
| `400` | `invalid_grant` | The Provider rejected the assertion, including an expired Feishu token or identity mismatch, or PAS could not map its user. |
| `400` | `invalid_scope` | The requested scope is not valid for the target resource. |
| `400` | `invalid_target` | The resource or requested/default Agent is invalid or unauthorized. |
| `401` | `invalid_client` | External application client authentication failed. |
| `413` | `invalid_request` | The form body exceeds the token endpoint limit. |
| `429` | `rate_limit_exceeded` | The token endpoint rate limit was exceeded. |
| `503` | `temporarily_unavailable` | Provider introspection or UserInfo is temporarily unavailable. |

For an introspection adapter, PAS first submits the external assertion as the
`token` parameter to the configured introspection endpoint. An active response
authenticates the token. PAS may then call the separately configured
`user_info` endpoint with `Authorization: Bearer <external-access-token>`.
The response must contain `sub`, `user_id`, and `pas_source_id`; it may also
contain `union_id`. `sub` must exactly match the Introspection response. The
current introspection identity mapping requires the source ID to identify the
configured active, verified enterprise identity source, and `user_id` must
already map to a PAS User in that source. Provider scopes remain in the
Provider's permission namespace and are not interpreted as PAS `mcp` or
`polarrag` scopes; the external application, resource, Workspace, and Agent
grants control PAS authorization.

PAS ensures the User Workspace exists. PAS access tokens default to 28800 seconds (8 hours),
are configurable from 60 to 86400 seconds, and never outlive a known external
token expiry. A temporary provider outage returns a temporary failure without
treating the token as invalid.

The Introspection and external UserInfo endpoints can use HTTP only when they
resolve exclusively to approved private-network ranges. This is intended for
trusted, isolated intranet deployments. Because PAS-to-provider credentials
and the external token are then sent without transport encryption, HTTPS is
still recommended and is required for public endpoints. Link-local and cloud
metadata targets remain blocked. This exception does not relax the HTTPS
requirements for interactive browser SSO endpoints.

For legacy clients that can only send a Bearer Token to `/mcp`, an administrator
may separately enable direct MCP external tokens. This compatibility mode is
off by default. PAS validates and maps the external token on every request,
does not issue a PAS refresh token, and never forwards the external token to an
Agent, Tool, MySQL instance, or PolarRAG service. Use Token Exchange when the
client can call `/token`; it keeps provider validation out of the normal MCP
request path after PAS issues an access token.

## Personal MCP and legacy workspace access

Personal Web resource discovery and `/mcp/personal` resolve the authenticated User directly and require no Agent. Personal tokens use the personal audience and do not authorize console REST or legacy `/mcp`. See [Accounts and resources](accounts-and-resources.md).

The workspace behavior below applies to existing `/mcp` and external integration paths.

### Legacy workspace and Agent relationship

An OAuth or console identity maps to a PAS User. Each User has one workspace,
and that workspace selects one default active Agent. The User must already be
authorized for the Agent through a direct assignment, PAS department,
enterprise group, or synchronized identity-source group. The Agent's MySQL
and PolarRAG bindings then determine the tools and resources visible to the
user:

```text
User -> User Workspace -> Default Agent -> MySQL / PolarRAG bindings
```

This means the User does not directly own an MCP database-tool surface.
The User is authorized to use an Agent, and the Agent owns the resource
bindings. PAS still applies the User identity and ACL when a user-scoped tool
requires it.

If exactly one active Agent is available, PAS selects it automatically. If
several are available, the user selects the default under **My Instances > MCP
connections**. If access to the selected Agent is later revoked, PAS retains
the unavailable selection and rejects tool execution until the user explicitly
chooses another Agent; it never switches resources silently. The workspace
states are `ready`, `selection_required`, `no_agent_access`, and
`default_agent_unavailable`.

This release does not automatically create a MySQL or PolarRAG instance for a
new user. An administrator must authorize an Agent and bind the required
resources. Existing Agent Tokens and `pas_user_agent_` tokens remain supported
for machine and manually configured clients.

## Agent authentication

Each Agent has one independently managed Token. The Token authenticates the
Agent identity at `/mcp`; it does not create an administrator session. Revoke
or regenerate it after exposure and reconnect the MCP client so its tool list
and authorization snapshot are refreshed.

## Failure handling

Repeated authentication failures should be investigated through sanitized
application and audit logs. Do not ask users to paste tokens, cookies, OIDC
secrets, or database URLs into public issues.
