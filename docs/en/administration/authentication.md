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

## Agent authentication

Each Agent has one independently managed Token. The Token authenticates the
Agent identity at `/mcp`; it does not create an administrator session. Revoke
or regenerate it after exposure and reconnect the MCP client so its tool list
and authorization snapshot are refreshed.

## Failure handling

Repeated authentication failures should be investigated through sanitized
application and audit logs. Do not ask users to paste tokens, cookies, OIDC
secrets, or database URLs into public issues.
