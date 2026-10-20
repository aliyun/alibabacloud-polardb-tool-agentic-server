# Enterprise identity sources

[简体中文](../../zh-cn/administration/enterprise-identity-sources.md)

An enterprise identity source imports a Feishu or SharePoint tenant's directory
facts for enabled PolarRAG Spaces. PAS encrypts every supplied secret with
`PAS_ENCRYPTION_KEY`. Feishu changes arrive through an outbound long
connection with conditional full reconciliation; SharePoint keeps the configured
polling interval. PAS constructs `acl_context` on the server, and PolarRAG
remains the final authority for document access.

The console **Integration guide** first offers separate **Feishu integration**
and **SharePoint integration** links. Selecting one shows only that provider's
configuration steps; use `A−`, `A`, or `A+` at the top of the page to adjust
the reading size. The identity-source list shows the PAS identity-source UUID
below each source name and provides a copy action for use in API integrations.

## Before you begin

Prepare the following:

- a PAS administrator account;
- a registered PolarRAG instance with the target Space enabled; and
- a provider application registered in the enterprise that owns the documents.

Each verified identity source can be bound to multiple enabled Spaces. A
Space's `identity_domain` comes from the PolarRAG Space catalog; it is not
operator input and does not need to equal the Space ID.

When adding an identity source, the console selects **Bind all Spaces,
including future Spaces** by default. PAS creates the parent binding for every
currently enabled Space and binds later Space registrations for sources that
retain this default policy. Clear the option when the source should use only
relationships explicitly created in the **Bind Space** dialog.

Unreadable or malformed stored source configuration disables automatic Space
binding and emits a warning without logging credentials. Existing bindings are
unchanged. Legacy configuration without the binding-policy field retains the
default; a present policy value must be a boolean.

## Configure a SharePoint identity source

A SharePoint source synchronizes Microsoft Entra directory users, groups, and
their direct user or nested-group memberships through Microsoft Graph. The
same Entra app also provides passwordless SharePoint sign-in for PAS users.

SharePoint integration has three steps: configure the external PAS address,
register and authorize the app in Microsoft Entra, then return to PAS to create
the identity source.

### Step 1: Configure the PAS external base URL

PAS is **PolarDB Agentic Server**, the management console and authentication
service for the current MCPServer. To enable SharePoint sign-in, open
**Settings** > **Configuration** > **Service runtime policy** in PAS. Set
**External base URL** to the HTTPS PAS address accessible from users' browsers,
run the check, and activate the configuration. For example:

```text
https://pas.example.com
```

Directory-synchronization-only testing has no redirect URI because PAS calls
Microsoft Graph directly. For SharePoint sign-in in testing or production, this
base URL must use the same host, scheme, and port as the Entra callback in the
next step.

### Step 2: Register and configure the app in Microsoft Entra

First identify the environment that hosts the Entra tenant:

| Environment | Tenant type | App-registration portal |
| --- | --- | --- |
| Global cloud | Global Azure / Microsoft 365 tenants | [Microsoft Entra admin center](https://entra.microsoft.com/) |
| China cloud (21Vianet) | Microsoft 365 China tenants operated by 21Vianet | [Azure China portal](https://portal.azure.cn/) |

In the portal chosen in the previous step, open **Microsoft Entra ID** > **App
registrations** > **New registration** and select **Accounts in this
organizational directory only (single tenant)**. On completion, copy the
Directory (tenant) ID and Application (client) ID from **Overview**.

Then open **Certificates & secrets** > **Client secrets** > **New client
secret**, enter a description, select an expiry, and create it. Immediately
copy the new secret **Value** into the PAS identity-source form's **Client
secret** field; do not use the secret ID. The value is shown once only. Create
a replacement secret and update PAS before it expires.

Before configuring the source, set **External base URL** in **Settings** >
**Configuration** > **Service runtime policy** to the PAS address with the
same host, scheme, and port as the sign-in callback below, then activate the
configuration. A production example is:

```text
https://pas.example.com
```

In the Entra app registration, open **API permissions** > **Add a permission**
> **Microsoft Graph** > **Application permissions** and add the following
permissions. Then select **Grant admin consent for <tenant name>** and confirm
that both permissions show as granted:

| Permission | PAS use |
| --- | --- |
| `User.Read.All` | List Entra users and their stable object IDs. |
| `GroupMember.Read.All` | List groups and their direct memberships. |

For directory-synchronization testing only, leave the optional **Redirect URI**
empty during registration; PAS calls Microsoft Graph directly. Any test of
**Sign in with SharePoint**, including offline testing, must use
**Authentication** > **Add a platform** > **Web** with a complete HTTPS
callback URL:

```text
https://pas.example.com/auth/sharepoint/login/callback
```

For an app that is already registered, open **Authentication** and select its
**Web** platform. Use **Add URI** to register another PAS callback, or edit an
existing entry to replace it. Save the platform after each change. Each
redirect URI must be unique; remove an obsolete callback instead of adding the
same URI again.

The base URL and redirect URI must use the same externally reachable host,
scheme, and port as PAS; do not enter only the PAS root URL. Microsoft Entra
rejects a non-localhost HTTP callback before authorization begins
(`AADSTS500117`). A **Public client/native (mobile and desktop)** registration
may accept that address in the portal but cannot be used by PAS.

#### Offline testing and production deployment

Offline testing that verifies only directory synchronization does not need a
redirect URI. Offline testing of SharePoint sign-in must first expose PAS over
HTTPS through a browser-trusted certificate, reverse proxy, or test domain,
then set that HTTPS base URL in both PAS and Entra. Production uses the same
**Web** platform and HTTPS callback configuration.

### Step 3: Create the SharePoint identity source in PAS

In **Users** > **Enterprise identity sources**, select **Add source** >
**SharePoint**. Choose the same **Global cloud** or **China cloud (21Vianet)**
environment as step 2, then enter the Entra directory (tenant) ID, application
(client) ID, and client-secret **Value**. After creation, select **Sync users and groups**
until the status is `active`.

PAS exchanges those credentials for an app-only Graph token against the
selected cloud, then stores only the encrypted source configuration and
synchronized directory facts. It marks the source `active` only after a
complete snapshot is written; a failed refresh is `stale`. Its last complete
snapshot remains usable until the configured grace period expires.

SharePoint user and group principals use Entra object IDs. The document ingest
pipeline must therefore write the same object IDs into PolarRAG ACLs. PAS does
not guess from email, user principal name, display name, or SharePoint
site-local group IDs.

After the source becomes `active`, users can select **Sign in with SharePoint**
on the PAS login page. PAS validates the authorization-code response with PKCE,
the Entra signing key, issuer, audience, and nonce. The Entra object ID claim
(`oid`) is the stable PAS enterprise user ID; a new user is created without a
password on first sign-in. Existing PAS users can instead be associated with a
synchronized SharePoint identity through **Bind enterprise identity**.

## Configure a Feishu identity source

Feishu integration has two steps: configure PAS in PolarDB Agentic first, then
configure the app in Feishu Open Platform.

### Step 1: Configure PAS in PolarDB Agentic

PAS is **PolarDB Agentic Server**: the management console and authentication
service for the current MCPServer. **External base URL** is the address exposed
to MCPServer users. Copy the current console URL from the browser, then remove
the page path (such as `/login` or `/users`), query string, and fragment so
only the scheme, host, and port remain. For example:

```text
https://pas.example.com
```

Sign in to PAS, open **Settings** > **Configuration** > **Service runtime
policy**, set **External base URL** to this value, run the check, and activate
the configuration. Production must use an HTTPS address reachable by users'
browsers.

### Step 2: Configure the app in Feishu Open Platform

Create and publish an internal Feishu app in the
[Feishu Open Platform](https://open.feishu.cn/app). In **Web application**, set
the **Desktop home page** to the PAS base URL from step 1. Then open **Security
settings** > **Redirect URLs** and add **both** callback URLs:

```text
https://pas.example.com/auth/feishu/tenant-verification/callback
https://pas.example.com/auth/feishu/login/callback
```

The `tenant-verification` callback verifies and activates the enterprise
identity source. The `login` callback completes passwordless Feishu sign-in
for individual PAS users; omitting it causes Feishu error `20029`.

The base URL and redirect URL must use the same externally reachable host,
scheme, and port as PAS. Do not use the callback URL as the desktop home page.
The redirect URI must use HTTPS in production.

In **Release management** > **Create or modify version** > **Availability**,
add the administrator, the administrator's department, or **All members**.
The user who verifies the source and signs in to PAS must be within the app's
availability scope. Publish the version after changing this setting.

In the Feishu app, open **Development configuration** > **Permission
management**, select **Contacts**, select **Configure**, and set the data scope
to **All members**. Then grant these **application identity** permissions:

| Permission | PAS use |
| --- | --- |
| `contact:contact.base:readonly` | Directory baseline. |
| `contact:department.base:readonly` | Department tree. |
| `contact:department.organize:readonly` | Department parent relationships. |
| `contact:user.base:readonly` | Tenant users and direct department user lists. |
| `contact:user.department:readonly` | Read `department_ids` on user events to update department membership. |
| `contact:user.employee_id:readonly` | Tenant-scoped `user_id` ACL identifier. |
| `contact:group:readonly` | Contact user groups and their membership. |

These seven permissions provide the fields needed for incremental directory
synchronization. After changing permissions, publish the app version and
manually trigger a full sync in PAS to restore missing memberships. If a user
response omits `department_ids`, PAS marks that incremental event as failed
instead of treating the missing field as an empty department list.

Open **Event subscriptions**, choose **Use long connection to receive events**,
and add these events before publishing the app version:

- `contact.user.created_v3`, `contact.user.updated_v3`, and
  `contact.user.deleted_v3`;
- `contact.department.created_v3`, `contact.department.updated_v3`, and
  `contact.department.deleted_v3`; and
- `contact.scope.updated_v3`.

The connection is initiated from PAS to Feishu. Do not configure an event
callback URL, Verification Token, or Encrypt Key, and do not expose PAS to
Feishu for event delivery. User events update the affected user and memberships;
department events read the department's direct users and update memberships.
A user deletion event disables the synchronized directory identity and its
dedicated PAS account, and revokes its refresh sessions; it does not delete the
account or disable a PAS-managed account linked to that identity. Full
reconciliation applies the same rule to users no longer present in Feishu.
If a departed user later returns, syncing the active directory identity
automatically re-enables its dedicated PAS account.
A scope event requests a full reconciliation. Feishu has no
equivalent contact-group event family or group change-log cursor. PAS therefore
reconciles only contact groups and their memberships every hour by default;
this does not scan all users or departments. An explicit full sync or fallback
after the configured incremental-unavailability threshold repairs missed-event drift.
Full and group-only reconciliation enumerate both contact-group types, then page
each group's direct members; they do not query group membership twice per user.

The document ETL already expands document ACL departments, contact groups, and
chats, and persists their canonical user memberships in
`acl_principal_membership`. It also stores optional `acl_group` membership.
PAS uses this snapshot to supplement directory API facts; users, departments,
and contact groups are synchronized directly from Feishu.
When configured, user events re-read that user's ETL memberships, and group-only
reconciliation retains ETL chat memberships.
The app used for directory synchronization can differ from the document-ingest
app, but both must belong to the same Feishu tenant and use tenant-level
`user_id` values.

### Create, verify, and bind a Feishu source

In the PAS console, open **Users** > **Enterprise identity sources**:

1. Select **Add identity source**, choose Feishu, and enter only the name, App
   ID, and App Secret. **Bind all Spaces** is selected by default: it binds all
   currently enabled Spaces and automatically binds Spaces registered with PAS
   later. Clear it to manage bindings individually. PAS then redirects the
   administrator to Feishu.
2. Sign in and authorize the app. PAS consumes a one-time state value, obtains
   the verified `tenant_key`, and returns to the console. It never stores the
   administrator's user access token.
3. PAS synchronizes users, departments, department hierarchy, and contact
   groups directly from Feishu. Assigning an Agent to a department includes
   users in its descendant departments. Select **Permission data source**
   to configure optional ACL membership and knowledge-scope snapshots.
4. Select **Bind Space** and manage the relationship in the paginated
   **Available** and **Bound Spaces** tabs. Both tabs support selecting or
   clearing the current page. Bind selected rows from the available tab and
   unbind selected rows from the bound tab. The next ACL evaluation no longer
   contributes this source's principals for an unbound Space. An explicit API
   unbind is not recreated by merely reading the console page.

Until tenant verification completes and the first directory refresh succeeds,
the source remains pending and contributes no ACL principals. A source binding
makes synchronized principals eligible in that Space; it never grants access
to a document by itself. After tenant verification, a Feishu user who has not
yet appeared in the completed directory snapshot can still sign in: PAS
creates that user's enterprise identity during the first successful Feishu
login.

If tenant verification did not complete, first confirm that the active
**External base URL** and the Feishu **Redirect URLs** entry exactly match the
callback URL above. Then select **Verify Feishu tenant** again; each attempt
uses a new one-time state value.

For automated identity-source, user-mapping, and Space-binding administration,
see the [Enterprise identity source administrator API](../reference/enterprise-identity-sources-api.md).
Keep secrets in a secret manager or protected runtime environment, never in a
URL or repository.

## Verify synchronization

PAS starts a verified source immediately. After the first complete Feishu
refresh, PAS consumes supported user and department events as the long connection
delivers them; queued events are normally processed within five seconds, subject
to provider and API latency. It checks fallback eligibility every ten minutes
by default, and starts a full reconciliation only after incremental updates have
been unavailable for the configured threshold (24 hours by default) and the
last full refresh is at least that old. A connection failure gets up to three
connection attempts. If all fail, PAS reports `error` and stops immediate reconnection;
periodic group and knowledge permission reconciliation continues. At the threshold, it again tries to connect (up to
three attempts) before the full reconciliation. The full run proceeds even if
connection fails, and the subscription remains in `error`. A `contact.scope.updated_v3` event
also requests a full reconciliation. Independently, PAS scans only contact
groups and their members, and configured ETL knowledge permissions, every hour by default. Group-only failures retain the
last complete directory snapshot and are retried without marking the whole
source stale.
Failed full reconciliations retry after backoff, including manual and startup runs.
PAS requests one full reconciliation on startup for sources not in `error` to recover changes missed while
the service was offline. A full and a group-only reconciliation never run at
the same time for one source: a requested full run waits for the group run to
finish. User and department events can be applied during a group-only run;
events received during a full run remain queued and are applied idempotently
after it completes.
SharePoint continues to use
the configured synchronization interval. An incomplete or failed full refresh
resumes from its provider cursor in the background. Feishu **Sync users and groups** returns
immediately and shows **Full sync in progress**; the background job finishes
already received incremental events before the full refresh. Selecting **Sync users and groups**
while a source already runs does not create a second run. The administrator
API's refresh, status, and failure semantics are documented in the
[Enterprise identity source administrator API](../reference/enterprise-identity-sources-api.md).

The **Incremental subscription** column reports `connecting`, `active`, or
`error` (legacy `connected_unconfirmed` also displays as active). An established
connection is healthy even without events; silence does not trigger a full
reconciliation. Every subscription status shows the last full-sync time, or
"Never" when no full sync has completed. In `error`, PAS also shows the sanitized
subscription warning, waits for the configured unavailability threshold, then tries to connect
before running a full reconciliation. If connection fails,
the status stays `error` and the next full run remains threshold-gated. After
fixing Feishu, operators can select **Sync users and groups** without waiting. Group and knowledge
permission reconciliation continues on its own interval even while the subscription
is in `error` and the full-reconciliation threshold has not elapsed.
The contact-list pagination token is not a replayable change cursor; PAS does
not rely on a "last sync version" to recover events. It establishes the
connection before the full snapshot and queues events arriving during that run.
PAS removes queued event records after seven days. If an unprocessed event expires,
it requests a full reconciliation before removal so the change is recovered from the provider snapshot.

The **Enterprise identity synchronization** module in the PAS Dashboard configures
`feishu_check_interval_seconds` (default `600`),
`feishu_fallback_after_seconds` (default `86400`), and
`feishu_group_and_knowledge_scope_interval_seconds` (default `3600`). These are respectively a
fallback scheduler check, an unavailability threshold for full reconciliation,
and a group and knowledge permission reconciliation interval—not an interval for polling Feishu
user or department changes. `interval_seconds` remains the SharePoint full
refresh interval (default `1800`). Activate the configuration change to apply
it at runtime.

The first Feishu full refresh uses two concurrent requests by default; later
full reconciliations use one, while SharePoint uses eight. Administrators can
change the SharePoint interval and Feishu limits in **Enterprise identity
synchronization** on the configuration page. In a Docker deployment, set
`PAS_FEISHU_INITIAL_SYNC_CONCURRENCY`, `PAS_FEISHU_SYNC_CONCURRENCY`, or
`PAS_SHAREPOINT_SYNC_CONCURRENCY` to an integer from `1` to `32` when capacity
requires a different limit. Different identity sources can run in parallel,
while each source has one database-leased in-flight run across PAS replicas.
Consecutive Feishu or SharePoint HTTP 500/503 responses back off from one
minute up to 30 minutes; HTTP 429 honors a bounded numeric `Retry-After`.
Other failures wait for the configured synchronization interval. A manual refresh returns
after its request budget and continues in the background when a full directory
cannot finish in time. Each completed provider page is committed to the
configured PAS metadata database immediately. The persistent `/var/run/pas`
checkpoint stores only provider cursors and pending work, never the full
directory; it is removed only after a complete refresh commits.

`active` means the latest snapshot completed. `stale` means the latest refresh
failed; PAS keeps the last complete snapshot usable during the configured
per-source grace period, seven days by default. The administrator can change
that grace period while editing the source. After it expires, PAS excludes the
source until a refresh succeeds. `last_error` contains only a sanitized error
type. A successful partial Feishu membership refresh instead records a separate
`sync_warning` with a stable code, skipped-group count, and provider-code counts.
The dashboard shows the warning and PAS preserves those groups' previous
memberships; the next complete warning-free refresh clears it. Check protected
PAS logs by time; never log or share app secrets or snapshot credentials.

The same freshness rule applies to external Token exchange (Feishu UserInfo and
introspection plus UserInfo), login, Agent membership, ACL context, and source-backed
PERSONAL owner mapping. An unexpired `stale` source does not by itself block token
validation; upstream token, tenant, subject, and audience checks still apply.
Stale sources without a successful snapshot, expired `stale` sources, and disabled or
pending sources remain unavailable. Source responses expose `snapshot_usable` so
the external application test dialog uses the server's freshness decision.

For a test user, compare the tenant-level `user_id` and intended principal IDs
with the PolarRAG document ACL. PAS emits `user`, `department`, `group`, and
`acl_group` principals. IDs are opaque and case-sensitive. Source bindings and
successful synchronization do not bypass PolarRAG Space, knowledge-base,
owner, or document ACL checks.

## Login and editing

Directory synchronization creates or updates passwordless PAS users. Users can
select **Sign in with Feishu** on the PAS login page; PAS validates the
one-time authorization response against the verified tenant and creates the
user when it is new. Do not assign passwords to synchronized identities.

For every enterprise identity source, sync keeps dedicated PAS accounts aligned
with the directory: a missing or disabled directory user disables the account
and revokes its refresh sessions; an active directory user enables it again.
PAS status changes to dedicated accounts are overwritten by the next sync.
PAS-managed accounts linked to an enterprise identity remain under manual
administration.

Automatic account disabling applies only to a canonical OIDC account owned by
that source and having exactly one external identity. Linked local accounts,
accounts with another identity, and accounts owned by another source keep their
PAS status and sessions. Removal revokes console refresh tokens, OAuth refresh
tokens and external token sessions; reactivation does not restore revoked tokens.

When a provider has one usable identity source, its sign-in action redirects
directly to that provider. A recently synchronized `stale` source remains usable
within its configured snapshot lifetime. When it has multiple usable sources, PAS first
shows a source-selection page; choose the enterprise identity source before
continuing the provider sign-in. PAS binds the selected source to the one-time
state. The callback never accepts a user-supplied source or tenant selection.

The source table exposes synchronized users and groups. Editing a Feishu
source requires the App ID and App Secret again and restarts tenant
verification. Deleting a source revokes its directory data, Space bindings,
and Agent group assignments, but keeps existing PAS users.

Editing a SharePoint source requires its tenant ID, client ID, and client
secret again. Existing SharePoint user sessions remain valid, while the next
login uses the updated app registration configuration.

## User identity mapping and PolarRAG access

The **Users** table shows the tenant-level external `user_id` beneath the
user's name and the identity source in its own column. Its **Departments**
cell combines local PAS departments with read-only enterprise departments and
contact groups. `PAS` denotes a local account; a Feishu or SharePoint label
denotes a synchronized enterprise identity. An empty organization cell means
the source did not return a membership for that user.

PAS always adds the user's stable `polarrag:user` alias to the server-built
`acl_context`. A synchronized user receives an alias based on its PAS external
ID. When an administrator binds that enterprise identity to an existing PAS
user, PAS also retains the synchronized account's source-generated native alias
for that source's bound Spaces. Existing Personal knowledge bases and documents
owned by the synchronized account therefore remain available to the mapped PAS
user, while the same request also carries the Feishu or SharePoint user and
group principals. PAS does not project either alias to an unbound or expired
identity source.

Use **Bind enterprise identity** only to associate an existing local PAS
account with one synchronized enterprise user. This administrator-managed
mapping can be edited or removed. The same panel also permits an administrator
to add, disable, or delete manual Feishu, SharePoint, or native PolarRAG
principals for that PAS user. Do not map by email or display name; select the
synchronized identity so PAS uses its stable external user ID.

After binding, the synchronized account remains visible in the **Users** table.
Its **Enterprise identity** panel continues to show the identity as
PAS-managed together with the target PAS user's native alias. Removing the
mapping returns the identity to synchronized mode.

## Agent group access

For normal setup, use **Configure enterprise access** on the Agent detail page.
The reviewed operation selects one active Source, synchronized groups or PAS
users, active PolarRAG instances, and at least one enabled Space from each
selected instance. Applying the preview atomically creates missing
Agent-instance bindings and Source-Space bindings. The preview distinguishes
Agent-local grants, global changes, and existing relations that will be reused.

PAS evaluates current membership for every Agent access check. Removing a user
from a group, disabling the group, or a stale source removes group-derived Agent
access immediately. Removing a specific user, group, or all-users grant from
the Agent does not remove global Source-Space bindings, the Agent-instance
binding, or shared PUBLIC scope.

The **Identity source · All synchronized users** option is an explicit,
separately removable grant. It is displayed first and is never preselected. It
includes the source's current and future active
synchronized users only while the source remains active and fresh; it does not
include unrelated PAS users and it does not bypass PolarRAG document ACLs.
The console supports selecting multiple individual users or groups. **Select
all groups** deliberately excludes the all-users identity-source option, so a
source-wide grant always requires an explicit administrator choice.

The existing identity-source Space page and Agent user/group controls remain
available for advanced, fine-grained administration.

## Configure the knowledge permission data source

For a verified Feishu source, select **Permission data source** in the source table.
Choose MySQL (default port `3306`) or PostgreSQL (default port `5432`), and enter the
host, database, read-only username, and password. The default database is
`polar_rag_meta`. PAS needs SELECT access to `acl_principal_membership`,
`external_kb_scope_snapshots`, and `external_kb_scope_grants`.

The password is encrypted with the PAS configuration secret key and never returned
by the API or populated in the form. When editing an existing connection, leave
Password blank to keep it, or enter a replacement. Saving preserves the identity source status, directory synchronization clocks, and
directory errors. It marks the independent ETL connection result as unverified.
Bind the intended Space, then open **Sync knowledge permissions**. Select **Verify
connection** to check SELECT access without importing permissions, or **Sync permissions now** to import the latest KB scope snapshots and reproject enabled Agents.
Connection results and permission import counts/times are recorded separately.
ETL failure does not mark the identity source stale or prevent directory login.
These actions do not synchronize contacts or create directory identities.
Only one ETL operation per source can run at a time; an operation is bounded to
120 seconds and an abandoned lease expires after 300 seconds. Configuration edits
are rejected while an ETL operation is running. **Refresh results** retries a
failed status read. Existing periodic directory/group synchronization is retained.

This connection synchronizes permissions for existing knowledge bases. It does not
ingest Feishu documents or create knowledge bases. Automatic Agent bindings require
an Agent in SCOPED mode, `external_knowledge_auto_bind_enabled` enabled, and an
existing global KB resource matching the snapshot's Space and KB IDs.

Administrators can open **Agent details → PolarRAG instances → Bind knowledge bases** and use **Automatically bind external knowledge bases**.
The switch is available only for SCOPED Agents; it is hidden for LEGACY_ALL Agents.
It shows the saved state and is off by default. The equivalent administrator API is
`PUT /api/admin/agents/{agent_id}/external-knowledge-auto-bind` with `{"enabled":true}`.
Enabling it projects existing
permission snapshots and includes later synchronizations, within the Agent's global
knowledge scope. It does not change the Agent's access mode: configure knowledge base
and user permissions first so scoped permissions apply. Disabling it removes this
Agent's synchronized scope/user bindings and preserves manual bindings. Loading or
saving failures are displayed; a failed save leaves the prior state visible.

The independent knowledge permission sync streams scope records directly and does not load directory memberships into memory. Connection verification only checks read access.

Memberships and scope grants are read in bounded keyset pages (1,000 rows per query)
for both dialects. Each paged read uses a read-only repeatable-read transaction;
a scope is imported only after all its grant pages have been read. A failed page
cannot publish a partial grant set. Memberships remain assembled for directory
reconciliation; at most one complete scope's grants are buffered during scope import.

The Dashboard configuration field is
`feishu_group_and_knowledge_scope_interval_seconds` (default `3600`, minimum `600`).
Existing configurations using `feishu_group_interval_seconds` remain readable;
new configuration responses and the schema use the new field name.


For a SCOPED Agent with auto-binding enabled, select **Update auto-binding**
on the knowledge base binding page. It replays PAS snapshots for this Agent only;
it does not connect to ETL, change the switch, or modify manual scopes. Fetch ETL
permissions on the identity source first when upstream permissions have changed.
User identity mappings, Agent user/group access, Space binding, and the Agent global
KB scope must already exist. The result shows processed/skipped snapshot counts
and projected binding counts; zero bindings may indicate missing access mappings.

### Discover a knowledge base through document permissions

Feishu ETL also publishes `document_read_discovery` scopes. A user with READ,
WRITE, or MANAGE permission on at least one successfully indexed document can
discover its existing KB without being a member of the containing Wiki space.
PAS imports these scopes through the same ETL permissions database connection.
Enable **Automatically bind external knowledge bases** on a SCOPED Agent and
include the target KB in its global scope. No additional database tables or
console switch are required.

Only active, current ACL generations confirmed as applied by Search contribute
grants. Pending, stale, deleted, or unsupported document ACLs do not. ETL
aggregates documents across Links; removing the last readable document removes
this discovery grant after the next ETL refresh and PAS scope reconciliation.
Independent Wiki space grants and manual bindings remain effective.

Discovery does not grant access to every document in that KB. PolarRAG continues
to enforce document permissions for every retrieval. Discovery is eventual: the
ETL job must refresh first, then PAS imports the scope on its configured interval
or **Sync permissions now**. An indexed document without a current confirmed ACL may remain
absent until ACL reconciliation succeeds.


### Permission refresh versus auto-binding

Periodic group/knowledge permission reconciliation first fetches Feishu groups,
then reads the configured permission database using the read-only snapshot client.
A Feishu API failure can therefore prevent the database read. Enabling or updating
Agent automatic binding only uses snapshots already imported into MCP; success
there does not verify upstream database connectivity or freshness. **Sync knowledge
permissions** fetches upstream scopes using the same database configuration and
connection client, without the Feishu directory/group API step or directory membership
assembly. It leaves the identity-source lifecycle state unchanged.

Group/permission failures use the configured group/knowledge interval for fallback
retry, rather than the full-directory fallback threshold. Existing excessive group
retry deadlines are bounded on a scheduler pass to one configured interval from that
pass. Active leases and valid backoff (including HTTP retry backoff up to 30 minutes)
are preserved. The repair changes only the group retry deadline, not source status
or successful sync clocks.

## Configure a DingTalk identity source

The user binding picker displays the registered DingTalk identity source name for users linked through DingTalk. This label is separate from the inherited permission origin shown on the binding.


When a Space has several identity sources, an Agent permission refresh processes each KB with the sources that have complete snapshots for it. A source with no upstream snapshot and no previously imported snapshot or derived binding for that KB is omitted. If a previously tracked source loses its snapshot, the entire KB refresh stops and retains existing bindings. An explicit complete snapshot with zero grants still revokes access. Skipped source IDs are recorded in each completed KB result.


If the sign-in endpoint returns HTTP 409, an administrator must first sign in to PAS, open **Enterprise identity sources**, select **Verify DingTalk enterprise**, complete browser authorization, and synchronize the directory. A pending source cannot start ordinary sign-in. After enterprise verification, **Synchronize users and groups** supports DingTalk through the same registered adapter as periodic reconciliation. The failure page provides these setup steps without exposing source configuration. DingTalk sessions also include the current credential epoch so an existing linked built-in account can authenticate.


Use an enterprise-internal application and ordinary employee accounts. In DingTalk,
configure browser authorization with the callback
`<PAS external base URL>/auth/dingtalk/callback` and authorize `openid corpid`.
First configure and activate `runtime_policy.external_base_url` with PAS's
externally reachable base URL. Verification and sign-in return HTTP 409 when
this URL is missing or invalid, without starting authorization or creating a
browser state cookie. Configure the URL and retry; PAS does not infer the
callback origin from browser or forwarded headers.
Grant application-visible directory scope and the APIs for unionId-to-userId,
user details, authorization scope, immediate department children and paginated
users. Enable Stream subscription for `user_add_org`, `user_active_org`, `user_modify_org`,
`user_leave_org`, `org_dept_create`, `org_dept_modify`, `org_dept_remove`.
See the [official Stream SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)
and [DingTalk authorization guide](https://open.dingtalk.com/document/orgapp/tutorial-obtaining-user-personal-information).

Create the **DingTalk** source with corpId as `tenant_id`, AppKey as `client_id`,
and AppSecret as `client_secret`. Verify the selected corporation in the browser,
then bind the target Spaces and synchronize the directory. The verified user token
must carry the configured corpId; PAS also proves internal employee membership
through application-authorized unionId conversion and user details. App changes
invalidate verification and old one-shot browser states. User tokens may be
exchanged only against the explicitly configured DingTalk trust source; callers
cannot supply a trusted userId or unionId.

PAS stores enterprise userId under `dingtalk:<corpId>`. Departments use native
numeric IDs as strings. USER, DEPT and ORG are supported; TAG, CONVERSATION and
DYNAMIC_GROUP do not grant discovered KB access. One `identity_domain` may bind
to only one DingTalk corporation. PAS local departments and manually assigned
principals do not substitute for DingTalk directory facts.

Directory reconciliation runs every five minutes and its maximum validity is
15 minutes, including while status is ACTIVE. Stream notifications are persisted and
deduplicated by source/event/subject before acknowledgment, then processed through
the same inbox, retry and transaction-fencing framework as Feishu. User events
suspend only the affected directory user and memberships; department events suspend
that department's inherited access. PAS refetches current facts and application
visibility rather than applying notification diffs. Delayed leave/delete events
cannot override a currently existing user or department. Missing IDs, unresolved
subtrees and exhausted event retries request a full reconciliation. Normal socket
rotation reconnects without increasing the error counter. Each newly established
connection requests a compensating full reconciliation for potentially missed events. Disabled/deleted source
listeners are released. Full scans cannot publish over newer events or changed credentials. Failed/partial
pages never publish a partial directory. Full scans are bounded to 50,000 entries;
restarts rescan rather than resume a partial cursor.

Configure the ETL permission database using a read-only account. PAS reads only
`source=DINGTALK` snapshots for the configured corporation. The ETL observation
marker must be within 15 minutes; unchanged permissions preserve their generation
and semantic update time. Document-read discovery requires already-applied Search
ACLs, completed nondeleted uploads and matching document IDs. Permissions are
aggregated per source/corporation/Space/KB; all links contribute. Agent membership,
SCOPED mode, explicit auto-binding and global resource scope remain required.
Discovery never grants access to every document; Search still checks document ACLs.
Use a synchronization interval of at most five minutes for the test link so
permission observations remain fresh.

### Maintenance deployment boundary

This adaptation is a **CONTRACT** schema release, generation 2. It extends released
provider CHECK constraints and adds browser-state/health storage. Ordinary managed
rolling migration refuses an existing older database. Stop every older reader and
writer, save a paired metadata/root-key/configuration backup, then run:

```shell
pas database inspect
pas database migrate --maintenance --operation-id dingtalk-identity-v1
pas database migrate --maintenance --operation-id dingtalk-identity-v1
pas database check
```

Test the complete path on an isolated database copy first. Old images deliberately
reject generation 2; rollback restores the pre-maintenance database and configuration
with the old image. New empty databases can initialize without a compatibility
window; completed migrations can be replayed. Do not use this branch for ordinary
managed rolling upgrades until a separately reviewed compatibility design exists.

Stream transport has bounded connection and shutdown timeouts. A connected socket is shown as **Subscription healthy**; failures are recorded as **Subscription error** and retried with exponential backoff up to 60 seconds. A full directory sync does not prove Stream health. Full reconciliation uses the shorter of the configured interval and 300 seconds, with 100 users per department page. Top-level authorized departments retain their actual names. Transient upstream network/429/5xx failures retain only an existing snapshot within its freshness limit; rejected snapshots and configuration errors invalidate it immediately. Incremental events never advance the full-directory freshness marker. An unresolved user event prevents that source's imported user grants and legacy manual group claims from bypassing suspension. Full reconciliation remains capped at 300 seconds to preserve the existing 900-second trust limit; Feishu's longer fallback schedule is not applied to DingTalk. Conflicting DingTalk corporations in one identity domain return HTTP 409 on both manual and automatic bindings.

### DingTalk compatibility and conversation boundary

DingTalk external users use the same directory freshness checks as Feishu users. External-token configuration preserves the `dingtalk` provider and a configured static identity source. Manual user-principal mappings accept `dingtalk`; identity-domain user lists include them. Manual DingTalk conversation groups are rejected before persistence; use identity-source directory departments for supported department access. This follow-up requires no schema migration.

ETL preserves `CONVERSATION` and `TAG` grants, but they do not confer knowledge-base discovery or produce inferred user memberships. `DYNAMIC_GROUP` fails strict normalization. Group metadata lookup success does not prove member-list access. Current application-token member queries remain blocked; no public reverse `openConversationId` to `chatId` API has been established.

Browser DingTalk login verifies identity only. It does not retain delegated DingTalk tokens or grant access to DWS Chat MCP. A future user-delegated resolver requires separate authorization, token lifecycle handling, complete member pagination and verified employee-ID mapping. It is outside the current acceptance scope.
