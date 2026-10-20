# Reusable Feishu API regression

**English** | [简体中文](feishu-regression.zh-CN.md)

This development fixture exercises the real Feishu directory client, PAS metadata
writes, group access evaluation and browser enterprise-verification callbacks.
All tenants, users, credentials and responses are synthetic. It requires no Feishu
account, cloud credentials or production database.

## Automated cases

From the repository root:

```shell
uv sync --locked --extra dev
uv run pytest tests/test_mock_feishu_provider.py -q
```

The reusable FastAPI provider is `mock_feishu_provider.create_app()`. Tests attach
it through `httpx.ASGITransport`; unexpected endpoints return an error. Cases cover
multiple pages, tenant `user_id` versus application `open_id`, nested departments,
user-group memberships, invalid credentials, missing pagination cursors, HTTP 503,
permission denial, callback binding, authorization-code replay, departed-user
access revocation, and preservation of the previous snapshot on fetch failure.

## Chrome console

```shell
(cd web && npm ci && npm run build)
uv run python scripts/dev/run_feishu_mock_console.py --confirm-local-development
```

Enter and confirm a temporary administrator password at the terminal prompt
(at least 8 characters). Input is hidden; the launcher refuses terminals that
cannot hide it. It does not print the password or mock App secret, or write a
plaintext credential file. Open `http://127.0.0.1:18765` in Chrome and sign in
with `admin` and the password you entered. Use `--port 18766` if needed. Each
invocation creates and migrates a new temporary SQLite database, creates its
administrator, sets its loopback public URL, and stores its database/checkpoints
in the temporary directory. Startup logs may also be written to the local `log/`
directory. Inherited PAS/cloud settings are discarded. Ctrl+C stops the server
and removes the temporary directory. Existing databases are never accepted.

Under **Access management > Create / manage accounts > Enterprise identity
sources**, create a Feishu source:

| Field | Value |
| --- | --- |
| Source name | Reusable Feishu Mock |
| App ID | `cli_mock_regression` |
| App secret | `mock-feishu-app-secret` |

Creation follows the local mock authorization redirect and the real PAS callback.
The tenant becomes `mock-feishu-tenant`. Select **Sync users and groups**, then
**Synced identities**: expect Alice Mock, Bob Mock, Research Mock, Platform Mock
and Reviewers Mock. Refresh after the asynchronous job completes.

Open `/login` and choose **Sign in with Feishu**. The mock authorizes Alice Mock;
the real PAS callback should open her member dashboard without administrator menus.
Sign in with the launcher's administrator again for further synchronization cases.

The provider is mounted only by this development launcher at `/__mock/feishu`.
Change scenarios with the following command, then synchronize again in Chrome:

```shell
curl --fail http://127.0.0.1:18765/__mock/feishu/__mock/scenario \
  -H 'Content-Type: application/json' -d '{"scenario":"removed_user"}'
```

| Scenario | Expected behavior |
| --- | --- |
| `normal` | Two users, nested departments and a user group synchronize. |
| `removed_user` | Bob disappears upstream; his directory-owned PAS account and inherited access are disabled. |
| `second_page_failure` | HTTP 503 on the second user page; no completed partial snapshot is published. |
| `missing_page_token` | `has_more=true` without a cursor is rejected. |
| `permission_denied` | Feishu code `99991672` is rejected. |

Return to `normal` and synchronize to test recovery. Inspect sanitized directory
request paths/query fields at `/__mock/feishu/__mock/status`. Tokens, secrets,
OAuth codes and state are excluded from that request history. Server access logs
can contain synthetic callback codes; treat launcher output as local test output.

## Contract sources and limits

Reference date: 2026-10-16. Official entry points:

- [Tenant access token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [Department users](https://open.feishu.cn/document/server-docs/contact-v3/user/find_by_department)
- [Child departments](https://open.feishu.cn/document/server-docs/contact-v3/department/children)
- [Department identifiers](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/contact-v3/department)
- [Common error codes](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)

The fixture implements the subset consumed by PAS, including user/group list and
detail calls. It forces one item per page to exercise pagination. Responses are
handwritten contract examples, not recorded tenant traffic or a complete Feishu
emulator. Some official documentation pages require client-side loading; the
mock's executable contract is explicit in its source and tests.

OAuth consent is automatic and codes expire after 60 seconds; redirects are
limited to the launcher's exact loopback PAS callback URLs. WebSocket subscription
is disabled in this launcher and its UI health is not an acceptance signal.
Outbound non-loopback networking is denied. Real tenant consent, application
visibility, provider rate limits, WebSocket events and network behavior still
require a tenant-backed integration environment.

Knowledge-scope snapshots come from the separate ETL permission database, not
these Feishu contacts endpoints. This fixture does not emulate that database,
PolarRAG search/document ACLs, DingTalk or SharePoint. Existing knowledge-refresh
tests cover synthetic snapshot projection separately; this Chrome recipe proves
the Feishu HTTP directory and browser-login boundary only.
