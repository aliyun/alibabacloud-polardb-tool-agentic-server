# Connect an MCP client

[简体中文](../../zh-cn/agents/connect-mcp-client.md)

Open **Connect MCP** for a personal connection. An administrator grants your personal account database access under **Resources** or **Access management**; no Agent is required. Knowledge uses your enterprise identity, ownership and upstream ACL.

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

OAuth clients must use this exact URL as the authorization `resource`; refresh retains the personal audience. Clients without OAuth can issue an expiring, one-time-visible `pas_personal_` token from **Token connection**. See [Accounts and resources](../administration/accounts-and-resources.md) for permissions, token lifecycle and rollback.

The remaining sections describe existing `/mcp` connections, which retain their Agent/workspace scope. They are available from **Connect MCP → Existing Agent connections and document upload**. Personal credentials cannot be used at `/mcp`, and legacy credentials cannot be used at `/mcp/personal`.

## Select the user's default Agent

PAS maps the signed-in identity to a PAS User and that User's workspace. The
workspace selects one active Agent, and that Agent owns the effective MySQL
and PolarRAG resource bindings.

- One available Agent is selected automatically.
- Multiple available Agents require a selection under **My Instances**.
- If the selected Agent is later disabled or access is revoked, PAS blocks
  tool execution until the user selects another available Agent. PAS does not
  silently switch resources.
- PAS does not create a MySQL or PolarRAG instance as part of login.

The available Agent list includes active Agents granted directly or through
the user's department, enterprise group, or synchronized identity-source
group.

## Browser OAuth flow

PAS supports Authorization Code and Refresh Token grants and requires PKCE
`S256`. The client may use dynamic client registration or a pre-registered
client. Register the callback exactly: remote callbacks use HTTPS; loopback
HTTP callbacks may select a dynamic port. PAS rejects a mismatched redirect
URI, issuer, resource, or audience.

The final request to `/mcp` still contains:

```http
Authorization: Bearer PAS_ACCESS_TOKEN
```

The important boundary is that PAS issued this token after validating the SSO
identity. Trusted external access tokens use the explicit exchange or direct
compatibility flows below; they are not silently treated as browser-login
credentials.

## Exchange an external access token

When a client already holds a trusted OIDC, OAuth, Feishu, or BUC access token,
the preferred integration is OAuth Token Exchange:

```bash
curl --request POST https://PAS_HOST/token \
  --user "${PAS_CLIENT_ID}:${PAS_CLIENT_SECRET}" \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode \
    'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  --data-urlencode "subject_token=${EXTERNAL_ACCESS_TOKEN}" \
  --data-urlencode \
    'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
  --data-urlencode 'resource=https://PAS_HOST/mcp' \
  --data-urlencode 'scope=mcp'
```

An administrator obtains `PAS_CLIENT_ID`, the one-time `PAS_CLIENT_SECRET`,
the endpoint, resource, scope, and a copy-ready request from
**External Applications**. The response contains a PAS access token and no
refresh token. Use the PAS access token for `/mcp`.

For an application with only the MCP target, `resource` and `scope` may be
omitted because PAS derives both values. Applications with both MCP and API
targets must send `resource`. Whether `agent_id` is accepted is controlled by
the application's Workspace-default, fixed-Agent, or caller-selectable policy.
`/api/v1/external-auth/token` is an equivalent API alias.

Do not omit `grant_type` from `/token`. The compatibility flow without a grant
type is not a token-endpoint extension. It is an optional `/mcp` behavior:

```http
Authorization: Bearer EXTERNAL_ACCESS_TOKEN
```

An administrator must explicitly enable direct MCP external tokens. PAS then
validates the external token at the configured provider on every request and
does not issue a PAS refresh token. This mode is useful for legacy clients but
adds provider latency and availability to every MCP request.

## Static Token compatibility

Machine integrations and clients without browser OAuth can still use the
Agent detail page's active `pas_agent_` Token. A user can also issue a
`pas_user_agent_` Token from **My Instances > MCP connections**. The copied
configuration has this shape:

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

Store static Tokens in the client's secret storage, not source control.
Built-in users reveal an existing Token only after password confirmation. SSO
users receive newly issued or regenerated plaintext once. Regeneration
immediately invalidates the old Token.

Private-network HTTP is suitable only for an isolated development environment.
Use HTTPS for production and any untrusted network.

## Network and TLS

The client must reach the external HTTPS URL, while PAS Pods must reach the
metadata database, registered MySQL endpoints, and any selected Alibaba Cloud
OpenAPI endpoints. Configure proxy and certificate trust according to the MCP
client documentation. Do not disable TLS verification in production.

## Refresh authorization

Tool visibility is computed from the workspace's selected Agent, Agent status,
and active bindings. Reconnect after changing the default Agent, resource
bindings, SQL capability, or a static Token. A connection established before
the change may retain an older tool list.

## Diagnose connection failures

Confirm the URL ends in `/mcp`. For browser OAuth, confirm SSO is active, the
browser callback completes, and the workspace status is `ready`. For Token
Exchange, confirm the PAS client is registered, external trust is active, and
the configured provider accepts the subject token. For direct external Bearer
authentication, also confirm the direct compatibility switch is active. For
static authentication, confirm the header uses `Bearer` and the Agent and
Token are active. Use sanitized server logs and Audit Logs; never paste a
Token into a public issue.
