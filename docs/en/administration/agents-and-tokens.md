# Agents and tokens

[简体中文](../../zh-cn/administration/agents-and-tokens.md)

An Agent is a non-human MCP identity with its own status, Token, direct
instance bindings, provisioning bindings, and owned resources.

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

## Review

Regularly review unused Agents, last-used timestamps, owned resources, and
Audit Logs. Revoke Tokens before decommissioning clients or staff automation.
