# Connect an MCP client

[简体中文](../../zh-cn/agents/connect-mcp-client.md)

Create an Agent and grant its access before connecting a client. The Agent
detail page is the source of truth for the MCP URL and active Token.

For PolarRAG, an administrator must also bind PolarRAG instances and assign the
PAS user to the Agent. The user then signs in and issues a `pas_user_agent_`
Token from **My Instances > MCP connections**.

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

The user-specific PolarRAG action copies the same JSON fields shown above; only
the Bearer value uses the `pas_user_agent_` credential. Built-in users copy an
existing Token only after password-protected reveal. For SSO users, issuing or
regenerating opens a one-time copy dialog with separate **Copy Token** and
**Copy JSON configuration** actions; closing it discards the plaintext.
Regeneration requires confirmation and immediately invalidates the old Token.
Setting an optional `expires_at` while issuing or regenerating does not add a
field to this client JSON.

Private-network HTTP is suitable only for an isolated development environment.
Use HTTPS for production and any untrusted network.

OAuth-capable MCP clients may use PAS dynamic client registration instead of a
manually copied Agent Token. Register the callback exactly: use HTTPS for a
remote callback or HTTP only for a loopback callback. PAS rejects a different
redirect URI during authorization and token exchange. The PAS issuer is read
from OAuth metadata and the access JWT; do not add it to the client JSON.

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
