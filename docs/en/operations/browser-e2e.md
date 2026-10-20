# Browser end-to-end tests

[简体中文](../../zh-cn/operations/browser-e2e.md)

The Playwright suite builds the console and starts a real PAS process with real
SQLite migrations for each case. Browser actions use real HTTP APIs, not mocked
responses. No existing server URL or database is accepted.

## Run locally

From the repository root:

```bash
uv sync --locked --extra dev
cd web
npm ci
npx playwright install chromium
npm run test:e2e
```

On Linux, use `npx playwright install --with-deps chromium`. The runner uses the
repository `.venv/bin/python`, falling back to `python3`; set `E2E_PYTHON` to
override the interpreter. Additional commands from `web/`:

```bash
npm run typecheck:e2e
npx playwright test --grep 'department'
npx playwright test --headed
npm run test:e2e:report
```

Direct `playwright test` commands require an existing `npm run build` output.
After upgrading Playwright, run `npx playwright install chromium` again to
install the matching browser revision. An executable-not-found error is a
missing test dependency; install it before rerunning the suite.

To test an installed Google Chrome instead, select its channel explicitly:

```bash
E2E_BROWSER_CHANNEL=chrome npm run test:e2e
```

The default remains Playwright's pinned Chromium. Chrome must already be
installed for the `chrome` channel; the runner does not silently substitute it.

## Coverage

| Case | Verified behavior |
| --- | --- |
| Fresh install | Claim, dry-run, activate administrator, login, dashboard, reload and service restart |
| Departments | Create, rename, reload and delete through the UI |
| Users | Create a member, sign in separately and reject admin access |
| Account layout | Persist long directory identities, contain the table at 1440/1024 px in English and Simplified Chinese, and open/close enterprise identity details |
| DingTalk callback | Without a public URL, return an actionable configuration error, keep verification retryable and avoid creating authorization state |
| Agents | Create and disable through the UI, persist after reload |
| Knowledge feature | Save a connection draft and block activation without verified access |
| Public boundary | Reject anonymous business/discovery requests and disabled online API documentation |
| OpenAPI endpoints | Require explicit network/region, preview official public/VPC and STS endpoints, reject catalog gaps, protect the authenticated catalog |
| OpenAPI configuration | Show safe failed-preflight diagnostics, invalidate edited plans, explicitly confirm activation, reuse stored credentials, persist after restart |
| Resource regions | Initialize new pools from active defaults, clear placement fields on region changes, constrain VPC selection, preserve existing pools and leave SQL-only instance region optional |

Unhandled browser JavaScript errors fail administrator cases. The dashboard
case captures a screenshot; this is functional UI coverage, not pixel-diff or
accessibility certification. Real upstream knowledge activation/search, cloud
provisioning, provider orchestration, external SSO, MySQL/PostgreSQL and
multi-replica deployment require separate integration suites.

OpenAPI cases use invalid disposable credentials and the real SDK. A Python
audit guard in `web/e2e/pas_runner.py` rejects non-loopback DNS/TCP/UDP before
network I/O; it does not fabricate PAS or upstream API responses. The cases
verify the connection-failure and explicit-override path, not successful cloud
authentication or VPC reachability. Pools use a zero prewarm target and no
worker is enabled. Successful live OpenAPI checks still require separate
acceptance from the deployed PAS network.

Backend SDK contract tests separately exercise successful and rejected OpenAPI
responses through a loopback HTTP mock. Native database, Compose and other
opt-in acceptance checks are described in the development
[local regression guide](../../../scripts/dev/local-regression.md).

## Isolation and diagnostics

Each case uses a random loopback port, temporary SQLite database, encryption
key and administrator password. Inherited `PAS_*` and `ALIBABA_CLOUD_*`
variables, inherited proxy settings, and `PYTHONPATH` are removed. The runner
permits only loopback network access; this is a test guard, not an OS security
sandbox. The fixture terminates only its own service and removes
only its own temporary data directory on teardown. No provider interface or
production setting is changed. Force-killing the runner can prevent cleanup.
Layout data is seeded directly into that private SQLite database; the seed
helper rejects other databases and paths outside the fixture's temporary
directory. Browser requests still read the persisted data through real APIs.

Failures retain screenshots and Playwright traces under `web/test-results/e2e`;
the HTML report is in `web/playwright-report`. Server logs redact the bootstrap
token. Traces can contain disposable test credentials: restrict artifact access
and never use real credentials in cases. These directories are Git-ignored.

The **Browser E2E** workflow runs Chromium on pushes and pull requests targeting
`main`/`develop`/`release/**`, and supports manual dispatch. Artifacts expire after seven
days. Retries are disabled so failures remain visible. Vitest continues to run
only `src/**/*.test.{ts,tsx}`.

Add cases in `web/e2e/`, using `pas` for an isolated server or `adminPage` for an
installation configured through the browser. Keep cases independent; do not
reuse state from another case or replace API responses with browser mocks.
