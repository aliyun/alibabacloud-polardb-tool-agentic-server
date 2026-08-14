# Authentication

[简体中文](../../zh-cn/administration/authentication.md)

PAS separates bootstrap ownership, human authentication, and Agent Tokens.
Credentials from one identity type cannot be substituted for another.

## Bootstrap and built-in login

The one-time bootstrap token is accepted only while the system is in setup
mode and is consumed when `core_admin` activates. The first administrator then
uses the built-in login. Session cookies require the CSRF header used by the
Web console; API clients should use the supported bearer flow.

Password changes and resets invalidate affected sessions according to the
active token-security policy. Never place bootstrap tokens or passwords in a
URL or configuration repository.

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

The `user_sso` module can remain `SKIPPED`. When enabled, configure an HTTPS
external base URL and OIDC provider metadata, client ID, encrypted client
secret, scopes, claims, and redirect behavior. Validate browser redirects and
logout in the production Ingress environment before enabling it for users.
Discovery metadata must contain the expected issuer. A manual endpoint
configuration must set `issuer`, `authorization_endpoint`, and
`token_endpoint` explicitly.

For MCP OAuth, PAS binds each OIDC callback to a short-lived, one-time state,
an OIDC nonce, and PKCE where the provider supports it. A failed or replayed
callback cannot be reused for a second MCP authorization.
PAS verifies the ID Token signature, issuer, audience, and nonce. When PAS
also reads UserInfo, its `sub` must exactly match the verified ID Token `sub`.
Validation failures return a generic, non-cacheable authentication error and
consume the one-time state.

## MCP OAuth security contract

PAS publishes dynamic client registration for MCP clients, but accepts only the
authorization-code and refresh-token grants with the `code` response type.
Every redirect URI is stored and compared exactly. Remote callbacks must use
HTTPS. HTTP is accepted only for loopback clients using `localhost`,
`127.0.0.1`, or `::1`; credentials, fragments, wildcards, custom schemes, and
unregistered redirects are rejected.

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

## Agent authentication

Each Agent has one independently managed Token. The Token authenticates the
Agent identity at `/mcp`; it does not create an administrator session. Revoke
or regenerate it after exposure and reconnect the MCP client so its tool list
and authorization snapshot are refreshed.

## Failure handling

Repeated authentication failures should be investigated through sanitized
application and audit logs. Do not ask users to paste tokens, cookies, OIDC
secrets, or database URLs into public issues.
