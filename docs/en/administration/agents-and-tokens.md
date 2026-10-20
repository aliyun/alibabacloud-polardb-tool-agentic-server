# Agents and tokens

[简体中文](../../zh-cn/administration/agents-and-tokens.md)

The console groups people under **Access management → Personal accounts** and Agents under **Service accounts (formerly Agents)**. Use **Resources** for database grants and **Connect MCP** for personal access without an Agent. Existing advanced controls and Agent connections described below remain supported. See [Accounts and resources](accounts-and-resources.md).

An Agent is a non-human MCP identity with its own status, Token, direct
instance bindings, provisioning bindings, and owned resources.

PAS has two distinct Agent-related credentials:

- `pas_agent_` authenticates the machine Agent and can receive database tools;
- `pas_user_agent_` authenticates one explicitly assigned PAS user through one
  Agent and can receive the authorized PolarRAG read, management, and upload
  tools supported by the selected Space.

They are independent credentials. A `pas_agent_` Token cannot impersonate a
user or call PolarRAG tools.

## Create and connect

Create an Agent with a descriptive name and purpose. The detail page displays
the active Token status, MCP service URL, and copy actions for the Token and a
JSON client configuration whose MCP server name defaults to the Agent name.
Copy it only into the intended client.

The copy actions reveal the active Token under the current authenticated
administrator session without asking for the PAS password again. Reveal remains
audited and rate-limited. The console writes the secret directly to the
clipboard, including through its private-HTTP fallback, and does not render it
on the page. Treat access to the Agent detail page and an unlocked administrator
session as secret access; do not capture the Token in screenshots, tickets, or
logs.

## Token lifecycle

Regeneration immediately invalidates the previous Token. Revocation blocks
authentication until a new Token is issued. Disabling an Agent blocks new
operations independently of Token status. Existing MCP sessions may retain an
older tool catalog, so reconnect after any status, Token, or binding change.

When a user issues or regenerates a `pas_user_agent_` Token, `expires_at` is
optional. Leaving it empty creates a Token with no fixed expiry. PAS does not
add `issued_at` and does not expire this Token because it was idle;
`last_used_at` is telemetry only. An explicit expiry, revocation, a disabled
user or Agent, or withdrawn Agent assignment is enforced on the next
authentication request.
An explicit `expires_at` must include a timezone offset. PAS converts it to UTC
before persistence; a datetime without an offset is rejected.

## Access bindings

Direct bindings select a registered instance, credential, permission, and
capabilities. SQL proxy access is optional and can expose `sql:read` and, with
`readwrite`, `sql:write`. Provisioning bindings apply only to healthy
`multitenant` backends and can expose `db_instance:create` without direct SQL
access.

An instance already bound to the Agent is excluded from the new-binding
selector. Remove or edit the existing binding instead of creating a duplicate.
The Agent detail page separates administration into **Database instances** and
**PolarRAG instances** tabs. Each tab repeats the same MCP connection details
for convenience; both use the same Agent Token and MCP endpoint. Database
bindings, REST connection details, provisioning routes, and resources stay in
the database tab. PolarRAG instance bindings and user or group assignments stay
in the PolarRAG tab.

## PolarRAG user connections

The Agent detail page organizes PolarRAG access into three steps: **Bind instances
and Spaces**, **Configure groups and users**, and **Bind knowledge bases**. The
first step has separate server-paginated views for active registered instances
that are not yet bound to the Agent and instances already bound to it, with batch
bind and unbind actions. The Agent page no longer exposes manual controls for global
Source-Space relationships. Those relationships are managed with the enterprise
identity-source configuration and remain available through the administration API
for inspection or removal.

The Source-Space relationship is the parent of user, group, and knowledge-base
grants. Removing one source from a Space removes that source's all-users and
enterprise-group knowledge grants in the Space. Grants from another identity
source and direct PAS-user grants remain while another source is still bound.
Removing the final source from a Space removes every lower-level knowledge grant
for that Space, so a stale grant cannot become effective again after rebinding.

The second step configures the enterprise identity sources, PAS users, and
Departments or groups available to the current Agent. Each tab can filter bound
or unbound rows, search, page, select the current page, and perform multi-row
bind or unbind operations. A
stale identity-source snapshot remains selectable only while the backend marks
that snapshot usable.

Agent and KB user pickers filter bindable candidates on the server before
pagination and search totals are returned. Disabled PAS accounts are excluded.
Enterprise users need an active directory identity in a usable source linked to
the current Agent's enabled Spaces (for KB selection, the selected KB Spaces).
An unexpired locally managed principal can supply an identity when no directory
record exists, but cannot override a disabled directory record. Multiple mappings
are evaluated by provider, tenant, external subject and PAS user ID, never by name;
one disabled identity does not exclude another valid in-scope identity. Built-in
accounts and ordinary OIDC accounts without enterprise mappings need no directory
record. Active sources and stale sources with an unexpired successful snapshot
remain usable.

Existing bound users remain visible and removable even when no longer eligible.
The UI marks them **Disabled** or **Identity unavailable**. User option responses
include `availability=active|disabled|unavailable`, separate from binding status
and PAS account status. Agent `binding_status=all` returns eligible candidates plus
existing bindings; `unbound` only returns eligible unbound users. KB `available`
excludes ineligible users, while `bound` retains their existing KB bindings.
This filtering does not change account status, delete identities or grants, or
revoke existing Tokens; those are separate administration operations.

**Bind all users** in step 2 joins every eligible enterprise identity source and
active built-in user to the current Agent, independent of search and pagination.
Eligible sources have a usable snapshot and a parent binding to an enabled Space
in an instance bound to this Agent. Source-wide membership includes future
synchronized users. Built-in users are covered by one dynamic Agent policy, not
per-user assignments: future active built-in users join automatically. New identity
sources still require another bind-all operation. Existing individual grants are
preserved during upgrade; run bind-all once to enable the dynamic built-in policy.
The built-in grant requires one policy update regardless of user count; summary
counts are computed by the database. **Unbind all users** requires confirmation and removes
all user, department, group, and identity-source memberships from this Agent,
revoking associated user Agent Tokens. Accounts, knowledge bases, other Agents,
and database-instance grants are not changed. Existing KB user policies remain;
group-assignment-specific KB grants are removed with their group assignment.
These actions are idempotent, including from partially bound states. Button
states refresh after individual or bulk changes. This is Agent membership, not
the KB-level `select_all` policy in step 3.

Agent access is the union of direct user, department, group, identity-source-wide
and built-in-user-wide grants. Removing a direct binding can leave inherited
access intact. The confirmation and result explain this, and the bound list shows
the remaining authorization source. Knowledge-base authorization and document ACLs
remain required. Migration `cf4a5b6c7d8e` adds the built-in-user policy disabled by
default; it does not infer dynamic authorization from historical direct grants.

Administrators can inspect `GET /api/agents/{agent_id}/all-subject-assignments`
and change memberships with `POST` to the same path and
`{"operation":"BIND"}` or `{"operation":"UNBIND"}`. Both return `status`
(`BOUND`, `PARTIAL`, or `UNBOUND`), `eligible_source_count`,
`eligible_user_count`, and `assignment_count`. The count includes the built-in
policy as one assignment. `BOUND` requires that policy and all eligible source
grants; historical direct user grants alone do not enable dynamic membership.
Bind-all is unavailable in `BOUND` state or when no eligible targets exist; unbind-all is unavailable only when no
memberships remain. A running legacy bulk user-assignment job causes `409`.
The Agent member section keeps **Bind all users** and **Unbind all users** side
by side, disabling each according to the current membership summary. Both are
disabled while loading or updating. PUBLIC/PERSONAL visibility controls also keep
bind and unbind side by side: fully bound disables bind, unbound disables unbind,
and partial binding enables both. KB all-user policies show only bind for unbound
targets and only unbind for fully bound targets; partial states use a split-button
with unbind in its menu. In the KB subject drawer,
**Available to bind** offers only **Add to selected KBs**, while **Bound** offers
only **Remove from selected KBs**. Actions are disabled while status is loading,
when nothing is selected, or when the selection is already in the target state.

The third step lists knowledge bases whose Space has a Source-Space parent
binding in one unified list. The legacy `management_mode` metadata does not filter
or restrict KB binding operations. Open **Manage user bindings** on any bound row;
the drawer title includes its name. Manual grants and external-sync grants are
maintained separately: removing a manual grant does not revoke an external grant,
and later synchronization replaces only its own grants. **Access scope** shows all Agent users, selected users /
groups configured, or not configured, with inherited grant sources shown below.
Selecting multiple rows reveals **Manage bindings in bulk**. Each subject shows
whether it is bound to every selected KB, some selected KBs, or none. **Add to
selected KBs** and **Remove from selected KBs** change only the selected
relationships, preserving other KBs, other subjects, and the Agent's scope mode.
Under select-all, removal records an exclusion without removing the all-users policy.
Outside select-all, removal affects direct manual KB bindings; inherited access is adjusted at its source. The drawer never removes Agent membership. Its Users, Departments
and groups, and Enterprise identity sources tabs each provide **Available to bind**
and **Bound** views based on the selected KBs, including subjects not yet assigned
to the Agent. Adding a binding also adds missing Agent membership. Enterprise
sources and directory groups remain restricted to usable identity sources bound to every selected
Space. Partially bound subjects appear in both views. Removing a direct binding
makes the subject available again unless inherited access still covers every KB.
The picker uses backend cursor pagination (previous/next), and the resource table
also uses backend pagination. Both support selecting and clearing the current
page. Changing tabs or filters clears old selections and action notices.
**Bind all users** stores a KB-level `select_all` policy covering current and future
Agent members without enumerating users. Removing a user, Department, directory
group, or identity source preserves `select_all` and records an exclusion.
Exclusions follow current group membership and take precedence over manual grants.
Binding the same subject again removes its own exclusion; an excluded parent group
or source must be restored separately. **Bind all users** clears all exclusions.
**Unbind all users** disables the KB-level default and clears manual grants only
for the selected KBs, without changing other KBs, Agent membership, or external-sync
grants. A KB override takes precedence over the Agent's `LEGACY_ALL` default.
When all selected KBs have a complete all-users policy, Bind all is disabled;
when none have grants, Unbind all is disabled. Both remain usable for partial
states. All operations are idempotent and refresh both the buttons and subject
lists. Ownership and upstream ACL checks remain mandatory.

`GET /api/admin/agents/{agent_id}/knowledge-resource-options` includes
`kb_type`, `agent_bound`, `access_scope` (`ALL_USERS`, `SELECTED`, or `UNCONFIGURED`) and
`inheritance_sources` for each row.
The optional `binding_status=bound|unbound` filter is applied before pagination
and counting; omitting it returns both sets. In **Bind knowledge bases**, the
Bound and Unbound views support selecting the current page and bulk unbinding or
binding native KBs respectively. Bound KBs retain individual and bulk user-binding
management. Changing filters or pages clears the selection. Manual binding operations do not edit external-sync grants.
`POST /api/admin/agents/{agent_id}/knowledge-binding-status` returns effective
`bound_subject_indexes`, directly removable `direct_bound_subject_indexes`, and
`inherited_subject_sources` keyed by the subject's zero-based request index.
Each `items` entry also includes `space_id` and `kb_id` alongside the existing
`knowledge_resource_id`. Both resource-ID and Space/KB request selectors return
the same identifiers; existing status fields and request ordering are unchanged.
`partial_subject_indexes` identifies partially excluded groups and `excluded_subject_indexes`
identifies excluded subjects. `SELECT_ALL` and `SELECT_ALL_EXCEPT` identify the KB policy.
`ALL_AGENT_KNOWLEDGE` identifies the inherited Agent-wide default. Status reads
do not create Agent assignments. Inherited Agent-wide defaults apply only to
Agent members, not every unassigned candidate.
Under the Agent-wide default or a KB's select-all policy, directory subgroups
also inherit membership through active ancestor groups assigned to the same
Agent and identity source. Binding status and the Bound/Available filters reflect
that inheritance without creating a direct subgroup assignment. Existing KB
exclusions still apply; content access continues to enforce owner and upstream ACL checks.
Exclusion-overlap status ignores disabled directory groups and paths through disabled
ancestor groups. Independent active paths and valid external principal grants remain effective.

`POST /api/admin/agents/{agent_id}/knowledge-subject-options` provides the KB drawer
candidate pages. Its body takes `knowledge_resource_ids` (1–100), `category`
(`users`, `groups`, or `sources`), `binding_status` (`available` or `bound`),
optional `search`, `offset` (initially 0), and `limit` (1–100, default 20).
The response contains `items` with `subject`, `label`, `identity_source`, `status`,
and `inheritance_sources`, plus `next_offset`. Send that cursor unchanged for the
next page; `null` means the end. `available` includes UNBOUND/PARTIAL subjects;
`bound` includes BOUND/PARTIAL subjects. This is a read-only admin operation.
User candidates are filtered in bulk before pagination; the endpoint does not
calculate a total count. Keep using `next_offset`, not the number of returned
rows, to advance. Restart at offset 0 when changing the search or binding filter.
The drawer debounces search input and discards responses from earlier searches
or drawer contexts. These optimizations do not change inherited grants,
select-all exclusions, or external-sync grant precedence.
The existing `/api/agents/{agent_id}/user-options` and `group-options` endpoints
also accept `binding_status=all` to include both membership states; their default
`unbound` behavior is unchanged.

Preview separates Agent-local grants, new global Source-Space bindings, and
relations already configured for reuse. Confirming a missing Source-Space
binding changes global shared state. Removing a user, group, or all-users grant
from this Agent removes only that Agent grant: global Source-Space bindings,
Agent-instance bindings, and shared PUBLIC scope remain. Instance permissions,
the PUBLIC scope summary, and instance removal are consolidated under step 1's
**Bound instances** tab; there is no separate bound-PolarRAG table.

Agent lists, assigned and unassigned user/group selectors, PolarRAG instance
selectors, Spaces, and PUBLIC knowledge-resource selectors use backend search
and pagination. Changing a search term resets the current page. This keeps the
Agent detail and enterprise-access dialogs responsive when tenants contain many
users, groups, Spaces, or knowledge bases.

Administrators can see assignment and Token status and can force-revoke a user
Token, but never receive its plaintext.

After signing in, an assigned user opens **My Instances**, then issues, reveals,
regenerates, or revokes their own Token in **MCP connections**. One assignment
has at most one active Token. Copy uses the current authenticated built-in or
SSO session without another password prompt; reveal remains owner-scoped,
audited, and rate-limited. Its effective knowledge scope is the intersection of
Agent-bound PolarRAG instances, resources visible to the PAS user, and the
document READ decision made by PolarRAG.

Agents default to `LEGACY_ALL` when no creation policy is specified. In `SCOPED` mode, PAS unions every matching
direct-user and group knowledge scope, then intersects that result with the
Agent-wide PolarRAG ceiling and the user's visible resources. Manual and
externally synchronized scopes can coexist; administrators can replace a scope
to change arbitrary user, group, or knowledge-resource bindings without
reissuing Tokens. PolarRAG document ACL remains the final authorization gate.
An enterprise identity-source-to-Space binding is not a prerequisite for
existing local-user or independently managed principal access. `LEGACY_ALL`
retains the Agent's instance and KB ceiling, subject to explicit KB policies
and exclusions. Source-backed ACL principals still require the corresponding
source-to-Space binding; unbinding it removes that source's contribution.
The **Bind knowledge bases** step on the Agent detail page lists these
relationships. Administrators can bind multiple assigned users, Departments,
or identity-source groups to multiple KBs in one operation, and can unbind any
selected subset. External-sync grants remain owned by synchronization; manual operations modify
separate manual grants.

The **3 Bind knowledge bases** page replaces Configure PUBLIC scope with separate
**Bind all / Unbind all** actions for PUBLIC and PERSONAL knowledge bases. These
actions change only the Agent's visibility ceiling across already-bound instances
and eligible Spaces, preserving per-user grants. The paginated KB table retains
unbound rows, labels the Agent-to-KB binding separately, and disables user management
until the KB is in the Agent's visibility scope. An unbound native KB provides
**Bind knowledge base** on its row, including after Unbind all. This restores only
that KB, preserves existing user policies, and refreshes the list and bulk status;
**Manage user bindings** then becomes available. Both bulk actions are idempotent,
including for partially configured scopes. Bind all includes eligible future KBs.
Existing PUBLIC limits are preserved until explicitly changed.

PERSONAL KBs remain visible only to their owner. Binding a PERSONAL KB to an Agent
does not share it with other users, and does not change personal Token access.
All discovery and resource-based MCP tools enforce the visibility ceiling, user
policy, ownership, and document ACL. Changes apply to the next Tool call without
regenerating Tokens.

The following administrator APIs manage the new controls:

- `POST /api/admin/agents/{agent_id}/knowledge-all-users-status` takes
  `knowledge_resource_ids` (1–100) and returns `items`, each with
  `knowledge_resource_id`, `space_id`, `kb_id`, `select_all`, `excluded_subject_count`, and
  `status` (`BOUND`, `PARTIAL`, `UNBOUND`).
- `POST /api/admin/agents/{agent_id}/knowledge-all-users` takes the same IDs
  plus `operation: BIND | UNBIND`. It is atomic and idempotent, and returns the
  refreshed status. External-sync KBs cannot be changed through this endpoint.
- `GET /api/admin/agents/{agent_id}/knowledge-visibility` returns
  `instance_count` and `statuses` for `PUBLIC` and `PERSONAL`.
- `POST /api/admin/agents/{agent_id}/knowledge-visibility` takes
  `kb_type: PUBLIC | PERSONAL` and `operation: BIND | UNBIND`, returning
  refreshed visibility. It never binds additional instances or Spaces.
- `POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` takes
  `knowledge_resource_ids` (1–100) and idempotently adds only these native PUBLIC
  or PERSONAL KBs to the Agent's visibility scope. All targets must be active,
  enabled, and within already-bound instances and eligible Spaces; otherwise the
  whole request is rejected with `422`. Returns the same visibility summary.
  Existing all-KB defaults, user policies, and owner/ACL checks are preserved.
- `POST /api/admin/agents/{agent_id}/knowledge-visibility:unbind` accepts the same
  body and validation rules, and atomically removes only the selected KBs from
  Agent visibility. Repeated requests are idempotent. User policies, Agent members,
  other KBs and database permissions are preserved. Unbinding from an all-KB
  default retains the dynamic default and records an exclusion for each selected
  KB. Future native and enterprise-sync KBs remain eligible; owner, user bindings,
  and upstream ACL still apply. Rebinding removes only the selected exclusions.
  Either all-type action clears that type's exclusions. Explicit allowlists
  remain explicit and are not automatically converted during migration.
  Exclusions apply only to their instance and the KB's current type, including in
  user-facing lists and cursor pages. If catalog synchronization changes a KB's
  type, rebinding it also clears that KB's stale exclusion from the previous type.

Instance binding responses include `public_excluded_resource_ids`; a null
`public_knowledge_resource_ids` means all PUBLIC KBs except these IDs. Replacing
the selection through the existing public-scope API also clears PUBLIC exclusions.
Historical explicit selections created by earlier single-KB unbinds cannot be
distinguished from intentional allowlists. To restore dynamic defaults, use
**Bind all** and then unbind the intended KBs again.

Existing `knowledge-bindings:batch` requests record or remove exclusions when a
target KB has select-all enabled. A repeated unbind creates no duplicate exclusion.

Deployments must run `pas database migrate` before starting this version and
retain a metadata backup. Earlier versions cannot enforce select-all exclusions
and are not compatible with the upgraded authorization data; do not start an
older binary against it.

## Review

Regularly review unused Agents, last-used timestamps, owned resources, and
the SQL and PolarRAG tabs in Audit Logs. Revoke Tokens before decommissioning
clients or staff automation.

### Choose the knowledge policy at creation

In **Create Agent**, choose **LEGACY_ALL** or **SCOPED**. The default is
LEGACY_ALL; SCOPED starts with no implicit user KB grants. For SCOPED, optionally
check **Automatically bind external knowledge bases** (off by default). Switching
back to LEGACY_ALL clears this checkbox. Bind the Agent resources, identity source
and Space before grants can be projected. You can later manage the auto-bind
switch in the unified **Bind knowledge bases** page for SCOPED Agents.
The auto-bind setting is hidden for existing LEGACY_ALL Agents. Document ACL still gates reads.

`POST /api/agents` accepts `knowledge_scope_mode` and
`external_knowledge_auto_bind_enabled`. Omitting them preserves LEGACY_ALL/false.
LEGACY_ALL with auto-bind enabled, or an invalid mode, returns 422 without creating
an Agent. Creation and detail responses include both saved values.


### Switch between SCOPED and LEGACY_ALL

In **Bind knowledge bases**, select **Switch to SCOPED** or **Switch to LEGACY_ALL**.
A mode change removes the Agent's previous external automatic binding scopes.
Explicit manual user/group bindings remain. SCOPED never copies implicit
LEGACY_ALL access. Returning to LEGACY_ALL disables automatic binding and restores
legacy defaults within the global scope, subject to existing manual overrides.
Document ACL and Agent access checks remain in force.

When entering SCOPED, optionally select **Automatically bind external knowledge
bases**. Imported MCP permission records are reprojected before the transaction
commits; the operation does not connect to the upstream permission database or
Feishu APIs. Mode, settings, removal and reprojection commit together; failure
rolls everything back. Readers see the committed previous or new state.
Reprojection work scales with imported grants and eligible Agent mappings, not
by expanding every identity-source member. Large permission datasets may increase
request time; wait for completion rather than retrying a pending operation.

The dialog exposes **Clear all All Agent users policies, including manually
configured ones**, selected by default. Review this choice: these policies have no
origin marker, so clearing them also removes manually configured all-user policies
and their exclusions. Uncheck it to retain those policies; they may continue to
make a KB visible after automatic permissions are revoked. Explicit manual
user/group bindings are retained either way. Existing SCOPED Agents can use
**Reapply SCOPED permissions** to reset historical all-user policies and rebuild
external bindings, without toggling modes twice.

The administrator-only `PUT /api/admin/agents/{agent_id}/knowledge-scope-mode`
accepts `mode`, optional `expected_mode`, `external_knowledge_auto_bind_enabled`,
and `reset_all_users_policies` (API default false). A stale expected mode returns
409. LEGACY_ALL rejects an enabled auto-bind flag or a policy reset with 422.


## Refresh external knowledge permissions

Administrators can request upstream permission-snapshot retrieval and Agent binding
publication in one operation. The Agent must use `SCOPED`, enable external knowledge
auto-binding, and include the selected active knowledge resources in its global range.

```http
POST /api/admin/agents/{agent_id}/knowledge-permissions/refresh
Authorization: Bearer <administrator-login-token>
Content-Type: application/json

{
  "identity_source_ids": ["<identity-source-id>"],
  "knowledge_resource_ids": ["<PAS-knowledge-resource-id>"]
}
```

Both lists are optional and must be nonempty if supplied. Send `{}` to refresh the
Agent's active global resource range with configured, usable Feishu sources bound to
the resources' Spaces. Selection does not depend on existing automatic bindings, so
newly granted users/resources can be discovered. A supplied resource must match at
least one selected source; every explicitly supplied source must contribute a target.
Self-managed resources without a matching permission source are skipped only when
resources were not explicitly selected. Source IDs identify PAS enterprise identity
sources, not Feishu application IDs. Resource IDs identify PAS knowledge resources,
not upstream `pkb-*` identifiers.

By default the response is `202` with `job_id`, selected IDs, `resources_total`, and progress.
For one explicit knowledge resource, set `wait_timeout_seconds` (0–30, default 0):

```json
{"knowledge_resource_ids": ["<PAS-knowledge-resource-id>"], "wait_timeout_seconds": 10}
```

Waiting requires exactly one distinct explicit resource ID (otherwise `422`).
Completion within the wait returns `200` with state `SUCCEEDED`; a terminal
`FAILED` or `PARTIAL` returns `409` for configuration conflicts, `413` for grant capacity limits,
or `502` for other failures, with its job state and sanitized error.
If the wait expires, the response is `202` with current progress and the same
`job_id`. Waiting or client cancellation does not cancel the durable task.
Server shutdown preserves checkpoints for recovery. This refresh imports the
latest available permission snapshots; it does not trigger upstream collection.
Poll the following administrator endpoint until a terminal state:

```http
GET /api/admin/agents/{agent_id}/knowledge-permissions/refresh/{job_id}
Authorization: Bearer <administrator-login-token>
```

States are `QUEUED`, `RUNNING`, `SUCCEEDED`, `PARTIAL`, and `FAILED`. `SUCCEEDED`
means all selected resources were processed, including skipped resources. `PARTIAL` means earlier KBs were
committed but a later KB failed; inspect `completed`, `current_knowledge_resource_id`,
and the sanitized `error`. `completed` records snapshot generations, grant counts,
fetch time, and publication time before commit; these timings are not a latency SLA.
A duplicate active request for the same Agent returns `409` and its existing job ID.

When snapshot generation and fingerprint are unchanged, existing shared grant rows
are reused; the selected Agent bindings are still recomputed from current access.

The worker retrieves all selected sources' complete snapshots for one KB before
writing PAS. All sources of that KB, replacement automatic bindings, obsolete scope
cleanup, and the checkpoint commit atomically. Manual bindings, other KBs, and other
Agents' binding rows are retained. Shared snapshot catalog entries are updated; other
Agents may use those latest snapshots in later refreshes. Multiple KBs publish
individually, not as one Agent-wide atomic cutover.

A failed, missing, stale, inconsistent, timed-out, or over-capacity snapshot does not
clear the previously committed bindings of that KB. Existing snapshot-validity rules
still apply. Changes to source configuration, source/Space association, Agent mode,
auto-binding, or global resource range are rechecked before publication. Permission
refresh does not modify identity-source directory lifecycle or synchronization clocks.

Tasks and per-KB checkpoints persist in PAS metadata. After process interruption,
the worker resumes unfinished KBs when the 180-second lease expires. Each KB has a
120-second fetch-and-publication deadline and a combined limit of 200,000 grants;
there are at most two refresh workers per server process. Larger work reports failure
rather than clearing existing permissions. This operation reads the configured
permission metadata database; it does not force Feishu collection, Search document-ACL
rebuilding, or directory-user/group synchronization. The original source refresh and
Agent snapshot-only reconcile endpoints remain available.

## Administrator query of complete knowledge access

```http
GET /api/admin/identity-sources/{identity_source_id}/agents/{agent_id}/users/{external_user_id}/knowledge-resources?access_origin=ALL&limit=200
Authorization: Bearer <administrator-login-token>
```

`external_user_id` is the user identifier (for example an employee number) stored
by that identity source. The source only maps this identifier to a PAS user in
`ALL` mode; it does not restrict where their grants originate. `ALL` reuses the
Agent's effective knowledge scope, combining manual user/group bindings,
external synchronization from any source, all-Agent-users policies and
`LEGACY_ALL` defaults. Agent admission, active user/Agent status, global range,
policy exclusions and resource visibility still apply. This is KB-level access;
document ACL filtering remains enforced during search and reads.

The default is `access_origin=ALL`. Use `access_origin=EXTERNAL_SYNC` to query
only external-sync grants from the specified source. Use `access_origin=MANUAL`
for explicit manual user/group bindings and all-Agent-users policies, applying
manual exclusions. MANUAL excludes external-sync grants and implicit
LEGACY_ALL defaults. A KB with both manual and external grants still appears in
MANUAL when its manual authorization is effective. `FEISHU` is no longer accepted
and returns `422`. Omitting the parameter now returns complete effective access. Responses retain `items` and `next_cursor`; follow
the cursor until null. Unknown user mappings or users without Agent admission
return an empty page; missing sources/Agents return `404`, and invalid origins or
cursors return `422`. This read-only query does not refresh permissions or issue
user tokens. The `/me/agents/{agent_id}/knowledge-resources` endpoint remains
external-sync-only.

### Refresh permissions in the console

For a SCOPED Agent with external auto-binding enabled, open its knowledge binding
page and choose **Refresh all knowledge permissions for this Agent** to fetch the latest available
permission snapshots and update automatic bindings across its global KB range.
Each bound KB row provides **Refresh permissions** for that KB. The top
**Refresh selected knowledge bases** action also accepts multiple selected KBs.
Single-KB requests wait up to 10 seconds, then continue asynchronously; multiple
selected KBs start asynchronously immediately. Whole-Agent refresh starts asynchronously immediately.

The page shows the task ID and completed/total KB progress and polls until completion.
After successful or partial completion, the binding list reloads. Failed KBs retain
previous bindings, and partial results identify incomplete work. If progress cannot
be read, retry the status query; this does not start another task. A task ID is retained
in the current browser session so returning to the page resumes progress checks.
A duplicate task response resumes the existing job. Leaving the page does not cancel
the server task. Switching mode, toggling automatic binding and imported-snapshot
recomputation are disabled while this page is tracking an active refresh.

**Recompute from imported permissions** is the secondary action: it uses only
snapshots already imported into PAS and does not fetch newer snapshots. Refresh does
not import documents or trigger upstream permission collection.

For single-KB, multi-KB, or whole-Agent refresh, a KB with no upstream permission
snapshot and no imported snapshots or derived scopes from any selected source is
skipped without modifying bindings. Progress counts processed KBs (including skips),
`resources_skipped` reports the skip count, and `completed` entries identify skipped
KBs and their reasons. Existing imported/derived permissions with missing snapshots
still fail and retain bindings. A safely skipped single-KB refresh returns `SUCCEEDED`, `resources_skipped=1`, and
the skip reason. Completion during bounded waiting returns HTTP 200; otherwise follow the 202 job. A valid zero-grant snapshot is processed normally.

### Refresh errors and task retention

Bounded waiting returns HTTP 409 for Agent, resource, or source configuration conflicts,
and 413 for grant capacity limits. Other terminal refresh failures return 502 with a job
object. An unrelated gateway error may return a non-job response; clients must validate
the job ID, Agent ID, state, and progress before tracking it. Queued jobs can exceed the
waiting window under load and return 202; status reads use backoff up to one second.

Batch processing stops at the first actual error. The current failed KB and later
unprocessed KBs retain their bindings; `resources_unprocessed` includes both. Previously
published updates remain effective. Skipped KBs count as processed but not published.
Only the latest task per Agent is retained when another task is accepted; queries for
the replaced terminal task return 404. Active tasks cannot be replaced.

`EXTERNAL_SYNC` returns effective synchronized access and applies active User/Agent,
Agent admission, global exclusions, personal-resource selection, and resource visibility.
The row refresh action also shows a notification on completion or failure.
When every target is skipped because no permission snapshot is available, the
summary and row notification show a warning that no update was applied; the API
state remains `SUCCEEDED`. A valid zero-grant snapshot still counts as applied.

Snapshot absence is an unknown state, not proof of revocation. Existing committed
bindings are retained on failure; revocation must be represented by a complete empty
grant or DENY_ALL snapshot. Document access still requires upstream ACL authorization.

After a permission refresh, the table keeps its existing rows while reloading the current page.
Successful reads replace the rows; a failed read reports an error, explicitly labels
the displayed rows as pre-refresh data, and asks the administrator to retry before further changes.
Changing Agent, filter, search, or page still clears the previous context.

### Refresh by upstream knowledge base IDs

```http
POST /api/admin/agents/{agent_id}/knowledge-permissions/refresh
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "kb_ids": ["pkb-example"],
  "space_id": "pks-example",
  "polarrag_instance_id": "<PAS-instance-ID>",
  "wait_timeout_seconds": 10
}
```

`kb_ids` accepts exact upstream PKB IDs (1–500 nonblank entries), as an alternative to
`knowledge_resource_ids`. The lists are mutually exclusive. `space_id` is the upstream
Space ID; `polarrag_instance_id` is the PAS registered instance UUID. Both selectors are
optional and only valid with `kb_ids`; they narrow every item in the request and never
expand the Agent scope. Identity source filters choose permission sources and cannot
resolve resource identity ambiguity.

Each distinct PKB must match exactly one active, enabled resource in the Agent global
scope after applying the optional selectors. No match returns 422 with
`KNOWLEDGE_RESOURCE_NOT_FOUND`; multiple matches return 422 with
`KNOWLEDGE_RESOURCE_AMBIGUOUS`. The entire request is rejected before a job is created
if any item cannot be resolved. Duplicate IDs are deduplicated. Selectors alone are
rejected; `{}` retains whole-Agent refresh. Existing internal-ID calls are unchanged.

The no-match error includes the applied `space_id` and `polarrag_instance_id`
selectors; a mismatched selector does not necessarily mean the resource is outside
Agent scope. If a resolved PKB has no selected configured identity source bound to
its Space, the request returns 422 with
`KNOWLEDGE_RESOURCE_NO_CONFIGURED_IDENTITY_SOURCE` and the affected `kb_id`,
`space_id`, and `polarrag_instance_id`; no partial task is created.

One distinct resolved resource supports bounded waiting; multiple resources are
asynchronous. Jobs checkpoint the resolved internal resource, instance, Space, and PKB
IDs. Publication checks the saved identity; recovery does not resolve the PKB again or
select a different target. Snapshot absence follows the existing single/multi-KB rules.
