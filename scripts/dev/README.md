# Local external OAuth Token Exchange test

This development-only setup tests an external OAuth integration without
requiring a real identity-provider tenant:

- PAS receives RFC 8693 Token Exchange requests at `/token`.
- PAS validates the external assertion through RFC 7662 Introspection.
- PAS calls a separate `user_info` endpoint with the same assertion.
- PAS maps `user_id` and `pas_source_id` to an existing PAS user.
- PAS issues its own access token without a refresh token.

The mock also exposes a minimal browser OAuth 2.0 Authorization Code +
UserInfo flow. That flow exists only because the current `user_sso`
configuration workflow requires a successful browser login test before
activation. It is separate from the external assertion validation contract and
does not make the external service an OIDC provider.

Use three terminals during the test:

| Terminal | Process |
| --- | --- |
| A | PAS on `http://127.0.0.1:18760` |
| B | Mock provider on `http://127.0.0.1:19090` |
| C | `curl` commands for Token Exchange and protected-resource calls |

The test has four distinct configuration objects:

1. **Service runtime policy** defines the public PAS origin used to construct
   callback, token, MCP, and API URLs.
2. **Identity source** provides the PAS namespace used to map the external
   `user_id` to an existing PAS user.
3. **User single sign-on** configures browser login and trusted external-token
   validation.
4. **External application** registers the service-to-service OAuth client and
   generates its PAS `client_id` and one-time `client_secret`.

## 1. Start PAS

Use the same bootstrap settings in every terminal:

```bash
cd /Users/cailu/github/alibabacloud-polardb-tool-agentic-server

uv sync --extra dev
mkdir -p data
if [ -s data/polardb_agentic.db ] && [ ! -f data/pas-root-key ]; then
  echo 'Existing PAS database requires its original data/pas-root-key' >&2
  exit 1
fi
if [ ! -f data/pas-root-key ]; then
  (umask 077; python3 -c \
    'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' \
    > data/pas-root-key)
fi
chmod 600 data/pas-root-key

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="file:$PWD/data/pas-root-key"

uv run pas database migrate
uv run pas database check
(cd web && npm install && npm run build)
uv run pas serve --local-sso-dev
```

PAS serves the built console at `http://127.0.0.1:18760`. The Vite server on
port `18761` is optional and must not be used as the PAS OAuth base URL.

`--local-sso-dev` is disabled by default. When enabled, PAS binds every
enabled listener to `127.0.0.1` and permits plain HTTP only for exact
`localhost`, `127.0.0.1`, or `::1` SSO and external-token endpoints. It still
rejects `0.0.0.0`, other `127/8` addresses, LAN addresses, hostname lookalikes,
credentials in URLs, and any hostname that resolves outside the loopback
interface. This mode is for same-machine testing only.

Complete initial setup and create the administrator before continuing.

## 2. Configure the local PAS origin

Use the loopback PAS origin in this terminal:

```bash
export PAS_BASE='http://127.0.0.1:18760'
```

In **Service configuration > Service runtime policy**, set **External base
URL** to that exact origin, without a trailing path. Save, validate, and
activate the module.

This is not the identity-provider or User SSO URL. It is the externally visible
base URL of PAS itself. PAS derives these values from it:

```text
Token endpoint: http://127.0.0.1:18760/token
MCP resource:   http://127.0.0.1:18760/mcp
API resource:   http://127.0.0.1:18760/api/v1
OIDC callback:  http://127.0.0.1:18760/auth/oidc/callback
```

The callback shown by PAS must now be:

```text
http://127.0.0.1:18760/auth/oidc/callback
```

Do not use this HTTP origin when PAS was started without `--local-sso-dev`.

## 3. Seed the local PAS identity mapping

Run this after PAS has started and the initial administrator exists. Replace
`admin` with the administrator's PAS user ID or `external_id` when necessary:

```bash
cd /Users/cailu/github/alibabacloud-polardb-tool-agentic-server

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="file:$PWD/data/pas-root-key"

uv run python scripts/dev/seed_mock_external_identity.py \
  --confirm-local-development \
  --pas-user admin
```

The command is idempotent and prints values similar to:

```text
PAS source ID:    01234567-89ab-cdef-0123-456789abcdef
PAS user ID:      ...
Provider key:     feishu:mock-external-tenant
External user ID: mock-directory-user
```

Save the printed source ID:

```bash
export PAS_SOURCE_ID='01234567-89ab-cdef-0123-456789abcdef'
```

The seed creates an active local identity namespace without provider
credentials. PAS can use it for external UserInfo mapping, while the background
directory synchronization ignores it.

The mapping succeeds only when all three values agree:

```text
Introspection subject == UserInfo subject
UserInfo pas_source_id == selected PAS identity source ID
UserInfo user_id == an active user in that identity source
```

After seeding, the new source is visible in the verified identity-source
selector under **User single sign-on > External Access Token trust**. If the
selector was already open, select **Refresh** before continuing.

## 4. Start the mock provider

In another terminal:

```bash
cd /Users/cailu/github/alibabacloud-polardb-tool-agentic-server

uv run python scripts/dev/mock_external_oauth_provider.py \
  --pas-source-id "$PAS_SOURCE_ID" \
  --user-id mock-directory-user
```

The default PAS-to-provider credentials are:

```text
client_id: pas-to-provider
client_secret: local-development-secret
```

Use the mock's loopback origin:

```bash
export MOCK_BASE='http://127.0.0.1:19090'
```

Check the route before configuring PAS:

```bash
curl --fail "$MOCK_BASE/healthz"
```

## 5. Configure and activate `user_sso`

Open **Service configuration > User single sign-on**.

Configure the browser login section:

| Field | Value |
| --- | --- |
| Provider protocol | OAuth 2.0 + UserInfo |
| Endpoint configuration | Manual endpoints |
| Issuer | `${MOCK_BASE}` |
| Authorization endpoint | `${MOCK_BASE}/oauth2/authorize` |
| Token endpoint | `${MOCK_BASE}/oauth2/token` |
| UserInfo endpoint | `${MOCK_BASE}/oauth2/login/user_info` |
| JWKS URI | empty |
| Client ID | `pas-console` |
| Client secret | `local-console-secret` |
| Provider name | `mock-console` |
| Scopes | `profile`, `email` |
| User ID claim | `sub` |
| Display name claim | `name` |
| Email claim | `email` |
| IdP PKCE | enabled |

Expand **External Access Token trust** and configure external-token
validation:

| Field | Value |
| --- | --- |
| Accept trusted external Access Token | enabled |
| Token validation Provider | OAuth 2.0 Token Introspection |
| Token Introspection endpoint | `${MOCK_BASE}/oauth2/introspect` |
| Introspection client authentication | `client_secret_basic` |
| PAS-to-Provider Client ID | `pas-to-provider` |
| PAS-to-Provider Client Secret | `local-development-secret` |
| External UserInfo endpoint | `${MOCK_BASE}/oauth2/user_info` |
| Verified identity source | the source created in step 3 |
| Expected external Token Audience | `polarrag` |
| PAS Access Token lifetime | `3600` |
| Allow external Bearer Token direct `/mcp` access | disabled |

Then perform the required sequence:

1. Select **Save and validate**.
2. Select **Test browser login**. The mock immediately redirects back to PAS.
3. Confirm that the test succeeds.
4. Select **Activate SSO**.

The module must show `ACTIVE` before an external application can be
registered. Saving a draft is not sufficient.

The browser OAuth fields above are only an activation fixture. A production
external-token integration uses the Introspection and external UserInfo
fields.

## 6. Register an external application

Open **External applications** and register a test application:

| Field | Recommended POC value |
| --- | --- |
| Name | `mock-external-client` |
| Target | MCP and HTTP API |
| Agent policy | Workspace default Agent |
| Secret expiration | empty, or a short test lifetime |

The Agent policy changes how Token Exchange selects resources:

| Policy | Token Exchange behavior |
| --- | --- |
| Workspace default | Do not send `agent_id`; the mapped user must have an available default Agent. |
| Fixed Agent | PAS always uses the configured Agent. |
| Caller-selectable | `agent_id` is optional; when omitted, PAS uses the workspace default Agent. |

For the workspace-default test, open **My instances > MCP connections** first
and confirm that the mapped PAS user has an active default Agent. PAS
automatically selects it only when exactly one Agent is available. When
multiple Agents are available, select one explicitly.

The application detail page is the source of truth for:

- `client_id`
- the one-time `client_secret`
- token endpoint
- resource and scope for each target
- whether `agent_id` may be supplied

The `client_secret` is shown only when the application is created or its secret
is rotated. Store it only for the duration of the local test. Do not place it in
source files, screenshots, issue descriptions, or shell-history transcripts.

Use the built-in **Test connection** action first with
`subject_token=mock-valid`. When both targets are enabled, select the MCP
resource for the first test.

A successful test proves that PAS can:

1. authenticate the external application;
2. introspect `mock-valid`;
3. fetch external UserInfo;
4. map the external user to the seeded PAS user;
5. resolve the configured Agent policy; and
6. issue a PAS access token for the selected resource.

## 7. Execute Token Exchange

Export the values copied from the external application detail:

```bash
export PAS_CLIENT_ID='copied-client-id'
export PAS_CLIENT_SECRET='copied-client-secret'
export PAS_RESOURCE="$PAS_BASE/mcp"
export PAS_SCOPE='mcp'
export EXTERNAL_ACCESS_TOKEN='mock-valid'
```

If both MCP and HTTP API targets were selected, `resource` is required. The
requested `scope` must match that resource.

Call the standard endpoint with the configured loopback MCP resource and save
the response:

```bash
TOKEN_RESPONSE="$(
  curl --silent --show-error --fail-with-body \
    --request POST "$PAS_BASE/token" \
    --user "${PAS_CLIENT_ID}:${PAS_CLIENT_SECRET}" \
    --header 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode \
      'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
    --data-urlencode "subject_token=${EXTERNAL_ACCESS_TOKEN}" \
    --data-urlencode \
      'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
    --data-urlencode "resource=${PAS_RESOURCE}" \
    --data-urlencode "scope=${PAS_SCOPE}"
)"

printf '%s\n' "$TOKEN_RESPONSE" | jq .
export PAS_ACCESS_TOKEN="$(
  printf '%s\n' "$TOKEN_RESPONSE" | jq -er '.access_token'
)"
```

For a caller-selectable Agent, add:

```bash
--data-urlencode "agent_id=${PAS_AGENT_ID}"
```

The compatibility endpoint can be tested by changing only the request URL to:

```text
http://127.0.0.1:18760/api/v1/external-auth/token
```

A successful response contains `access_token`, `token_type=Bearer`,
`expires_in`, `scope`, and
`issued_token_type=urn:ietf:params:oauth:token-type:access_token`. It must not
contain `refresh_token`.

The issued token must have the PAS origin as `iss`, the requested resource as
`aud`, a PAS user subject, and the requested scope. Its lifetime is limited by
both the configured PAS lifetime and any expiry reported by the external
provider.

## 8. Verify MCP access

Send an MCP `initialize` request and save the response headers:

```bash
MCP_HEADERS_FILE="$(mktemp)"

curl --silent --show-error --no-buffer \
  --dump-header "$MCP_HEADERS_FILE" \
  --request POST "$PAS_BASE/mcp" \
  --header "Authorization: Bearer $PAS_ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {
        "name": "external-token-poc",
        "version": "1.0"
      }
    }
  }'
```

The response must report protocol version `2025-03-26`, a `tools`
capability, and the PAS MCP server information.

Extract the optional session ID and list the tools visible through the mapped
user's default Agent:

```bash
export MCP_SESSION_ID="$(
  awk 'tolower($1) == "mcp-session-id:" {
         gsub("\r", "", $2)
         print $2
       }' "$MCP_HEADERS_FILE"
)"

MCP_SESSION_ARGS=()
if [ -n "$MCP_SESSION_ID" ]; then
  MCP_SESSION_ARGS=(
    --header "Mcp-Session-Id: $MCP_SESSION_ID"
  )
fi

curl --silent --show-error --no-buffer \
  --request POST "$PAS_BASE/mcp" \
  --header "Authorization: Bearer $PAS_ACCESS_TOKEN" \
  "${MCP_SESSION_ARGS[@]}" \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json, text/event-stream' \
  --data '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'
```

The returned catalog depends on the resources bound to the resolved Agent.
An empty or reduced catalog can therefore be a valid authentication result;
check the Agent's MySQL and PolarRAG bindings before treating it as an OAuth
failure.

## 9. Verify HTTP API access

Exchange a separate token for the HTTP API resource:

```bash
export PAS_RESOURCE="$PAS_BASE/api/v1"
export PAS_SCOPE='polarrag'

API_TOKEN_RESPONSE="$(
  curl --silent --show-error --fail-with-body \
    --request POST "$PAS_BASE/token" \
    --user "${PAS_CLIENT_ID}:${PAS_CLIENT_SECRET}" \
    --header 'Content-Type: application/x-www-form-urlencoded' \
    --data-urlencode \
      'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
    --data-urlencode "subject_token=${EXTERNAL_ACCESS_TOKEN}" \
    --data-urlencode \
      'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
    --data-urlencode "resource=${PAS_RESOURCE}" \
    --data-urlencode "scope=${PAS_SCOPE}"
)"

export PAS_API_ACCESS_TOKEN="$(
  printf '%s\n' "$API_TOKEN_RESPONSE" | jq -er '.access_token'
)"

curl --silent --show-error --fail-with-body \
  --request GET "$PAS_BASE/api/v1/tools" \
  --header "Authorization: Bearer $PAS_API_ACCESS_TOKEN" \
  --header 'Accept: application/json' |
  jq .
```

Do not reuse an MCP token for the HTTP API or an API token for MCP. Their
`aud` and `scope` values are intentionally different.

## 10. Verify failure behavior

Set `EXTERNAL_ACCESS_TOKEN` to each fixture and repeat the exchange:

| Assertion | Expected result |
| --- | --- |
| `mock-inactive` | `invalid_grant` |
| `mock-wrong-audience` | `invalid_grant` |
| `mock-subject-mismatch` | `invalid_grant` |
| `mock-source-mismatch` | `invalid_grant` |
| `mock-userinfo-not-found` | `invalid_grant` |
| `mock-provider-error` | `temporarily_unavailable` |
| `mock-userinfo-error` | `temporarily_unavailable` |

Also verify these PAS-side controls:

- wrong external application secret returns `invalid_client`;
- a resource outside the registered targets returns `invalid_target`;
- an unauthorized `agent_id` returns `invalid_target`;
- omitting `grant_type` from `/token` is rejected;
- disabling the external application blocks subsequent exchanges.

Common diagnostics:

| Error | First item to check |
| --- | --- |
| `invalid_client` | External-application `client_id`, current secret, and application status |
| `invalid_grant` | Introspection `active`, audience, subject equality, source ID, and user mapping |
| `invalid_target` | Registered resource, scope, Agent policy, and workspace default Agent |
| `temporarily_unavailable` | Mock-provider process and Introspection/UserInfo response |
| MCP `401` | Token expiry, MCP audience, and `mcp` scope |
| API `401` | API audience, `polarrag` scope, and resolved Agent authorization |

After testing, rotate or disable the external application. A secret pasted into
a terminal transcript, chat, screenshot, or issue must be treated as exposed
and rotated immediately. PAS access tokens from this flow expire automatically
and cannot be refreshed.

## Production replacement

When production provider endpoints are available, replace only the
mock-specific values:

- Introspection endpoint -> provider RFC 7662 endpoint.
- PAS-to-Provider client ID and secret -> credentials issued by the provider
  to PAS.
- External UserInfo endpoint -> provider `user_info` endpoint.
- Verified identity source -> the production source containing the same
  directory `user_id` values returned by the provider.
- Expected audience -> the audience returned by Introspection.

The browser SSO section must be configured with a real interactive identity
provider unless PAS later separates external token trust activation from the
browser SSO activation proof.
