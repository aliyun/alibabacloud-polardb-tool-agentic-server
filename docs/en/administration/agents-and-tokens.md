# Agents and tokens

[简体中文](../../zh-cn/administration/agents-and-tokens.md)

An Agent is a non-human MCP identity with its own status, Token, direct
instance bindings, provisioning bindings, and owned resources.

PAS has two distinct Agent-related credentials:

- `pas_agent_` authenticates the machine Agent and can receive database tools;
- `pas_user_agent_` authenticates one explicitly assigned PAS user through one
  Agent and can receive the authorized PolarRAG read, management, and upload
  tools supported by the selected Space.

They are independent credentials. A `pas_agent_` Token cannot impersonate a
user or call PolarRAG tools.

## Create and connect

Create an Agent with a descriptive name and purpose. The detail page displays
the active Token, MCP service URL, and a JSON client configuration whose MCP
server name defaults to the Agent name. Copy it only into the intended client.

The administrator view intentionally displays the active Token for operational
setup. Treat access to that page as secret access; do not capture it in
screenshots, tickets, or logs.

## Token lifecycle

Regeneration immediately invalidates the previous Token. Revocation blocks
authentication until a new Token is issued. Disabling an Agent blocks new
operations independently of Token status. Existing MCP sessions may retain an
older tool catalog, so reconnect after any status, Token, or binding change.

When a user issues or regenerates a `pas_user_agent_` Token, `expires_at` is
optional. Leaving it empty creates a Token with no fixed expiry. PAS does not
add `issued_at` and does not expire this Token because it was idle;
`last_used_at` is telemetry only. An explicit expiry, revocation, a disabled
user or Agent, or withdrawn Agent assignment is enforced on the next
authentication request.
An explicit `expires_at` must include a timezone offset. PAS converts it to UTC
before persistence; a datetime without an offset is rejected.

## Access bindings

Direct bindings select a registered instance, credential, permission, and
capabilities. SQL proxy access is optional and can expose `sql:read` and, with
`readwrite`, `sql:write`. Provisioning bindings apply only to healthy
`multitenant` backends and can expose `db_instance:create` without direct SQL
access.

An instance already bound to the Agent is excluded from the new-binding
selector. Remove or edit the existing binding instead of creating a duplicate.
The Agent detail page separates administration into **Database instances** and
**PolarRAG instances** tabs. Each tab repeats the same MCP connection details
for convenience; both use the same Agent Token and MCP endpoint. Database
bindings, REST connection details, provisioning routes, and resources stay in
the database tab. PolarRAG instance bindings and user or group assignments stay
in the PolarRAG tab.

## PolarRAG user connections

On the Agent detail page's **PolarRAG instances** tab, bind the permitted
PolarRAG instances and explicitly assign PAS users. Administrators can see
assignment and Token status and can force-revoke a user Token, but never
receive its plaintext.

After signing in, an assigned user opens **My Instances**, then issues, reveals,
regenerates, or revokes their own Token in **MCP connections**. One assignment
has at most one active Token. Its effective knowledge scope is the intersection
of Agent-bound PolarRAG instances, resources visible to the PAS user, and the
document READ decision made by PolarRAG.

By default, an Agent binding includes all PUBLIC knowledge resources across
enabled Spaces on its PolarRAG instance. An administrator can use
**Configure PUBLIC scope** on
the binding to narrow it to selected, synchronized, ACTIVE PUBLIC resources.
An empty selection excludes every PUBLIC resource. Saving **All PUBLIC
resources** restores the instance-wide default, including eligible PUBLIC
resources synchronized later.

PERSONAL resources never appear in this administrator selection. They continue
to follow the PAS user's enterprise-principal mapping, PolarRAG owner rules,
and document ACL. The PUBLIC selection is an additional ceiling: it can remove
resources from discovery and every resource-based MCP Tool, but cannot grant
access that the user or a document ACL does not already allow. Scope changes
take effect on the next Tool call for existing user-specific Agent Tokens; no
Token regeneration or MCP reconnect is required.

## Review

Regularly review unused Agents, last-used timestamps, owned resources, and
the SQL and PolarRAG tabs in Audit Logs. Revoke Tokens before decommissioning
clients or staff automation.
