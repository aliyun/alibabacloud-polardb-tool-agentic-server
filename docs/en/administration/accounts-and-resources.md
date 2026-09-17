# Accounts and resources

[简体中文](../../zh-cn/administration/accounts-and-resources.md)

An account answers **who is accessing PAS**. A resource answers **what it can
access**. Each account has its own allowed operations on resources.

| Concept | Meaning | Console entry |
| --- | --- | --- |
| Personal account | An existing PAS User, authenticated by SSO or local login | Access management → Personal accounts |
| Service account | An existing Agent representing an application | Access management → Service accounts |
| Resource | A database or an enabled knowledge catalog entry | Resources |
| Credential | Proof of identity, not a separate set of permissions | Connect MCP, or service account connection settings |

A service account's creator does not inherit its permissions. SSO is a login
method for a personal account, not another kind of account. Existing User,
Agent and binding identifiers remain unchanged; this feature needs no metadata
schema migration (`NONE`).

## Grant database access

1. Open **Resources → Databases**, select a resource and **Manage access**.
2. Choose **Add access**, a personal or service account, and an active
   `direct_access` database credential belonging to that resource.
3. Select **Query data** (`sql:read`) or **Query and write data** (`sql:read`,
   `sql:write`). Both include resource discovery and description. A credential
   with a read-only limit cannot grant write access.
4. Save. The same binding appears under **Access management → account →
   Manage access**. Either entry updates the same existing binding.

The SQL preset preserves independently configured credential-reveal and
provisioning capabilities. Those remain in **Advanced settings**, alongside
connection configuration, credentials, enterprise identities and capacity
management. Adding a SQL preset does not authorize provisioning.

The access table shows configured grants and their sources: direct grant,
system allocation or group inheritance. Execution additionally checks current
account, resource and credential state. Inherited grants are managed at their
source group; the existing group model supports multitenant allocations, not
arbitrary database credential grants. Editing a system allocation as an explicit
grant requires a valid direct-access credential.

**Disable access** retains a disabled binding. For a personal account, that deny
also blocks inherited access to the same resource; deleting the row could have
restored inherited permissions. Disabling database access does not delete data
or remove a service account's separate provisioning grants. Changes are audited
and the next request rechecks access.

## Connect a personal account

Open **My resources** to inspect your own database and knowledge access, then
**Connect MCP**. No Agent assignment or default-Agent selection is required.
Use the displayed address, for example:

```json
{
  "mcpServers": {
    "pas-personal": {
      "type": "http",
      "url": "https://PAS_HOST/mcp/personal"
    }
  }
}
```

OAuth clients discover
`/.well-known/oauth-protected-resource/mcp/personal` and use the personal URL
as the authorization `resource`. Authorization Code with PKCE and Refresh
Token retain this audience. A client that omits the personal resource gets the
legacy audience and cannot use that token at `/mcp/personal`.

For clients without OAuth, the **Token connection** tab issues one active
`pas_personal_` bearer token per user, valid for 1–365 days (90 by default).
Copy it from the one-time dialog and configure `Authorization: Bearer TOKEN`.
PAS stores only its hash. Listings never reveal plaintext. Revoke it before
issuing a replacement. Revocation, expiry, account disablement and credential
epoch changes invalidate it. Administrators cannot retrieve another user's
personal token. Personal tokens cannot authenticate console REST requests or
be exchanged as refresh tokens.

A personal token uses current personal permissions; it has no independent
resource scope. SQL-only personal grants also allow resource discovery and
description without revealing database passwords. Personal clients cannot
create or delete service-owned databases or select a service account.

SQL tools require an explicit `instance_id`; personal MCP does not expose branch or default-instance operations. If the external URL is unset, the console builds the Token connection URL from the current browser origin and marks OAuth unavailable until an administrator configures `runtime_policy.external_base_url`. After changing the external URL, revoke and reissue personal tokens.

## Knowledge access

Knowledge remains optional; enable it under **Advanced settings → Feature
settings** following [knowledge activation](../knowledge/activation.md).
Disabled knowledge disappears from resource discovery and MCP tools while
DB access remains available.

The resource page exposes knowledge configuration and permission sources.
Visibility continues to use enterprise identity and ownership. Each search or
document read sends the employee's resolved ACL context to the upstream service,
which enforces document permissions. There is no additional per-user knowledge
grant table or local override of upstream ACLs. Personal MCP supports knowledge
reads; existing delegated Agent connections continue to provide the configured
upload/management workflows.

## Existing connections and rollback

`/mcp`, `pas_agent_`, `pas_user_agent_`, workspace selection, and external token
exchange retain their existing semantics. **Connect MCP → Existing Agent
connections and document upload** opens the compatibility view. A personal
token cannot use `/mcp`; legacy credentials cannot use `/mcp/personal`.

No existing grant is copied or unioned into a personal account. Review and add
personal database grants explicitly when moving a person from a workspace
connection. Older images do not support the new personal endpoint/audience;
rolling back an image leaves the unchanged schema in place and users can keep
using their existing legacy connection. Do not begin relying exclusively on the
new endpoint until the target rollout is complete. Proxies must forward both
`/mcp/personal` and its protected-resource metadata path.
