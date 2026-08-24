# Enterprise identity sources

[简体中文](../../zh-cn/administration/enterprise-identity-sources.md)

An enterprise identity source imports a Feishu or SharePoint tenant's directory
facts for enabled PolarRAG Spaces. PAS encrypts every supplied secret with
`PAS_ENCRYPTION_KEY`, refreshes the source every five minutes, and constructs
`acl_context` on the server. PolarRAG remains the final authority for document
access.

The console **Integration guide** first offers separate **Feishu integration**
and **SharePoint integration** links. Selecting one shows only that provider's
configuration steps; use `A−`, `A`, or `A+` at the top of the page to adjust
the reading size.

## Before you begin

Prepare the following:

- a PAS administrator account;
- a registered PolarRAG instance with the target Space enabled; and
- a provider application registered in the enterprise that owns the documents.

Each verified identity source can be bound to multiple enabled Spaces. A
Space's `identity_domain` comes from the PolarRAG Space catalog; it is not
operator input and does not need to equal the Space ID.

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
(client) ID, and client-secret **Value**. After creation, select **Sync now**
until the status is `active`.

PAS exchanges those credentials for an app-only Graph token against the
selected cloud, then stores only the encrypted source configuration and
synchronized directory facts. It marks the source `active` only after a
complete snapshot is written; a failed refresh is `stale` and its principals
fail closed.

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
| `contact:user.base:readonly` | Tenant users and department membership. |
| `contact:user.employee_id:readonly` | Tenant-scoped `user_id` ACL identifier. |
| `contact:group:readonly` | Contact user groups and their membership. |

These five permissions are sufficient for PAS directory synchronization.

The document ETL already expands document ACL departments, contact groups, and
chats, and persists their canonical user memberships in
`acl_principal_membership`. It also stores optional `acl_group` membership.
PAS uses this snapshot only for principals unavailable from the directory API;
users, departments, and contact groups are synchronized directly from Feishu.
The app used for directory synchronization can differ from the document-ingest
app, but both must belong to the same Feishu tenant and use tenant-level
`user_id` values.

### Create, verify, and bind a Feishu source

In the PAS console, open **Users** > **Enterprise identity sources**:

1. Select **Add identity source**, choose Feishu, and enter only the name, App
   ID, and App Secret. PAS immediately redirects the administrator to Feishu.
2. Sign in and authorize the app. PAS consumes a one-time state value, obtains
   the verified `tenant_key`, and returns to the console. It never stores the
   administrator's user access token.
3. PAS synchronizes users, departments, and contact groups directly from
   Feishu. The console does not expose optional ACL membership snapshot
   configuration.
4. Select **Bind Space**, choose one or more enabled PolarRAG Spaces that
   should use this source, and confirm the selection. The same dialog lists
   existing bindings; select **Unbind** beside a Space to remove it. The next
   ACL evaluation no longer contributes this source's principals for that
   Space.

Until tenant verification completes and the first directory refresh succeeds,
the source remains pending and contributes no ACL principals. A source binding
makes synchronized principals eligible in that Space; it never grants access
to a document by itself.

If tenant verification did not complete, first confirm that the active
**External base URL** and the Feishu **Redirect URLs** entry exactly match the
callback URL above. Then select **Verify Feishu tenant** again; each attempt
uses a new one-time state value.

For automated identity-source, user-mapping, and Space-binding administration,
see the [Enterprise identity source administrator API](../reference/enterprise-identity-sources-api.md).
Keep secrets in a secret manager or protected runtime environment, never in a
URL or repository.

## Verify synchronization

PAS makes the first background attempt within five minutes and repeats every
five minutes. To refresh one ready source immediately, select **Sync now** in
the source table. The administrator API's refresh, status, and failure
semantics are documented in the
[Enterprise identity source administrator API](../reference/enterprise-identity-sources-api.md).

`active` means the latest snapshot completed. `stale` means the latest refresh
failed; PAS excludes that source from new ACL contexts until a later refresh
succeeds. `last_error` contains only a sanitized error type. Check protected
PAS logs by time; never log or share app secrets or snapshot credentials.

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

When a provider has one active identity source, its sign-in action redirects
directly to that provider. When it has multiple active sources, PAS first
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
group principals. PAS does not project either alias to an unbound or stale
identity source.

Use **Bind enterprise identity** only to associate an existing local PAS
account with one synchronized enterprise user. This administrator-managed
mapping can be edited or removed. The same panel also permits an administrator
to add, disable, or delete manual Feishu, SharePoint, or native PolarRAG
principals for that PAS user. Do not map by email or display name; select the
synchronized identity so PAS uses its stable external user ID.

## Agent group access

For normal setup, use **Configure enterprise access** on the Agent detail page
after its PolarRAG instance is bound. The reviewed operation selects one active
Source, synchronized groups or PAS users, and eligible Spaces together. Its
preview distinguishes Agent-local grants, missing global Source-Space bindings,
and existing relations that will be reused. Creating a missing Source-Space
binding changes shared global state.

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
