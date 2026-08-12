# Agents and tokens

[简体中文](../../zh-cn/administration/agents-and-tokens.md)

An Agent is a non-human MCP identity with its own status, Token, direct
instance bindings, provisioning bindings, and owned resources.

PAS has two distinct Agent-related credentials:

- `pas_agent_` authenticates the machine Agent and can receive database tools;
- `pas_user_agent_` authenticates one explicitly assigned PAS user through one
  Agent and can receive only the seven PolarRAG tools.

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

## Access bindings

Direct bindings select a registered instance, credential, permission, and
capabilities. SQL proxy access is optional and can expose `sql:read` and, with
`readwrite`, `sql:write`. Provisioning bindings apply only to healthy
`multitenant` backends and can expose `db_instance:create` without direct SQL
access.

An instance already bound to the Agent is excluded from the new-binding
selector. Remove or edit the existing binding instead of creating a duplicate.
The Agent detail page's **Instance access** table lists both database and
PolarRAG bindings and identifies each row with an **Instance type** column.

## PolarRAG user connections

On the Agent detail page, bind the permitted PolarRAG instances and explicitly
assign PAS users. Administrators can see assignment and Token status and can
force-revoke a user Token, but never receive its plaintext.

After signing in, an assigned user opens **My Instances**, then issues, reveals,
regenerates, or revokes their own Token in **MCP connections**. One assignment
has at most one active Token. Its effective knowledge scope is the intersection
of Agent-bound PolarRAG instances, resources visible to the PAS user, and the
document READ decision made by PolarRAG.

## Review

Regularly review unused Agents, last-used timestamps, owned resources, and
the SQL and PolarRAG tabs in Audit Logs. Revoke Tokens before decommissioning
clients or staff automation.
