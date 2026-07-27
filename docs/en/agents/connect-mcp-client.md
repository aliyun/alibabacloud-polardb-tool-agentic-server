# Connect an MCP client

[简体中文](../../zh-cn/agents/connect-mcp-client.md)

Create an Agent and grant its access before connecting a client. The Agent
detail page is the source of truth for the MCP URL and active Token.

## Copy the client configuration

The **Copy JSON configuration** action produces:

```json
{
  "mcpServers": {
    "AGENT_NAME": {
      "url": "https://PAS_HOST/mcp",
      "headers": {
        "Authorization": "Bearer AGENT_TOKEN"
      }
    }
  }
}
```

The server name defaults to the Agent name. Replace no fields manually when
using the console-generated JSON. Store the Token in the client's secret
storage, not source control.

## Network and TLS

The client must reach the external HTTPS URL, while PAS Pods must reach the
metadata database, registered MySQL endpoints, and any selected Alibaba Cloud
OpenAPI endpoints. Configure proxy and certificate trust according to the MCP
client documentation. Do not disable TLS verification in production.

## Refresh authorization

Tool visibility is computed from Agent status and active bindings. Reconnect
after granting or removing direct access, SQL proxy, provisioning capability,
or after Token regeneration. A connection established before the change may
retain an older tool list.

## Diagnose connection failures

Confirm the URL ends in `/mcp`, the header uses `Bearer`, the Agent and Token
are active, and system setup is complete. Use sanitized server logs and Audit
Logs; never paste the Token into a public issue.
