# Feature usage 3: Agent, Token, and MCP

**English** | [简体中文](../../zh-cn/getting-started/agents-and-mcp.md)

This page creates an Agent, issues a Token, grants it instance access, and
connects an MCP client to call the database instance tools.

## Create an Agent

Create an Agent in the console to represent the machine identity of an AI
application. Agents are separate from human users.

<p align="center">
  <img src="../../zh-cn/getting-started/images/agents-page.png" alt="Agents page" width="820">
</p>

<p align="center">
  <img src="../../zh-cn/getting-started/images/create-agent-form.png" alt="Create Agent form" width="820">
</p>

## Issue a Token

An Agent Token is issued automatically when the Agent is created and is used
to authenticate the MCP client. Click the Agent name to open its detail page,
where you can check the Token status and regenerate or revoke it at any time.

<p align="center">
  <img src="../../zh-cn/getting-started/images/agent-token-detail.png" alt="Agent Token detail" width="820">
</p>

## Grant instance access

Add instance access for the Agent under **Instance access**. Note:

- Only instances with status `active` or `stopped` can be selected; instances
  that are `creating` or `failed` are disabled in the dropdown and cannot be
  bound.
- Choose the direct-access credential, read/write permission, and capabilities
  (such as list, describe, credentials read, SQL, and create) as needed.

<p align="center">
  <img src="../../zh-cn/getting-started/images/agent-instance-access.png" alt="Add instance access for the Agent" width="820">
</p>

## Connect an MCP client

Click **Copy JSON configuration** to copy the generated connection config and
paste it into the MCP client. The config looks like:

```json
{
  "mcpServers": {
    "polardb": {
      "url": "http://<host>:18760/mcp",
      "headers": { "Authorization": "Bearer <agent-token>" }
    }
  }
}
```

For network, TLS, and reconnect details, see
[Connect an MCP client](../agents/connect-mcp-client.md).

## Call the database instance tools

Once connected, the Agent can call the four database instance tools:
`list_db_instances`, `create_db_instance`, `describe_db_instance`, and
`delete_db_instance`. For parameters, identifiers, and error semantics, see the
[Tool reference](../agents/tool-reference.md).

Taking MCP Inspector as an example, the authorized tool list appears after
connecting:

<p align="center">
  <img src="../../zh-cn/getting-started/images/mcp-inspector-tools.png" alt="Tool list in the MCP client" width="820">
</p>

For managing Agents, Tokens, and bindings, see
[Agents and tokens](../administration/agents-and-tokens.md).

## Review the access audit logs

Every statement an Agent executes through the SQL proxy is audited. Open the
**Audit Logs** page in the console to review Agent access by user, SQL type,
instance, and result. For audit scope and retention, see
[Audit and security](../administration/audit-and-security.md).

<p align="center">
  <img src="../../zh-cn/getting-started/images/audit-logs.png" alt="Agent SQL access audit logs" width="820">
</p>

Next: [Feature usage 4: resource pool and instances](./resource-pool.md).
