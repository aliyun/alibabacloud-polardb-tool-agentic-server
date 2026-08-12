# Auto-provisioning pools (AgenticDB Dedicated)

[简体中文](../../zh-cn/database-instances/dedicated-hot-pools.md)

Auto-provisioning pools keep verified PolarDB MySQL clusters ready for fast Agent
allocation. This is the only pool model in PAS; there is no separate legacy or
human-user pool. Registered instances remain in the instance inventory and
are assigned independently.

## Supply model

PAS supports exactly three ways to supply database access:

1. An administrator registers a `multitenant` instance, configures its
   high-privilege provisioning backend, and binds it to an Agent. The Agent
   can then create logical tenant/database resources automatically.
2. An administrator registers a `single-tenant` instance and assigns its
   direct-access credential to an Agent. The Agent uses that physical cluster;
   PAS does not purchase or replenish it.
3. An administrator creates one or more auto-provisioning pools with a target size.
   PAS uses the active Alibaba Cloud identity to purchase
   `AgenticDBType=dedicated` clusters and manages every purchased cluster as a
   member of the selected pool.

Human Users use only administrator-assigned registered instances. They do not
claim pool members or trigger physical purchases. If no registered instance is
assigned, the request returns `NO_INSTANCE_ASSIGNED` with guidance to contact
an administrator.

## Enable and create a pool

Run the additive metadata migration on every environment before enabling the
worker. In **Service Configuration → Service runtime policy**, set
`dedicated_pool_enabled=true`. Every PAS replica polls the active configuration,
and its in-process supervisor starts the auto-provisioning task without a
restart or rollout. Activating or rotating Alibaba Cloud credentials also
rebuilds the worker's cloud client online. When false, multitenant workers
continue but the auto-provisioning worker does not purchase, prepare, recheck,
synchronize, disconnect, restore, sanitize, or destroy members.

Open **Pool** and create an auto-provisioning pool. The page can contain
multiple pools; Agent routing determines which pool supplies each create
request. Configure:

- `target_size`: minimum planning capacity PAS tries to maintain.
- `max_total_members`: hard billable-member ceiling; it must be at least
  `target_size`.
- `max_member_purchases_per_hour`: durable, replica-safe purchase budget.
- `max_create_requests_per_agent_per_hour` and
  `max_delete_requests_per_agent_per_hour`: Dedicated-only Agent operation
  budgets.
- region, zone, VPC, VSwitch, and storage type. Enter these identifiers
  manually; PAS deliberately does not request additional RAM permissions to
  enumerate network resources. After entering the region, the wizard links to
  `https://vpc.console.aliyun.com/vpc/<region_id>/vpcs` so you can copy a VPC
  and VSwitch from the Alibaba Cloud VPC console. The VSwitch must belong to
  that VPC and zone, and PAS must be able to reach the private network.
- optional PAS private source CIDRs for the PolarDB whitelist. Enter the
  private ECS or Kubernetes source ranges of the PAS deployment that will
  perform grants and verification. Never use `0.0.0.0/0` or a local
  workstation's public address for development convenience.
- `destroy` or `sanitize_and_reuse` reclaim policy.
- permission-template revision and generated database/account name templates.
- optional pool `delete_cooldown_duration_hours`.
- Available health-check interval and maximum evidence age. Maximum evidence
  age must be at least twice the interval; defaults are 300 and 600 seconds.

Start with `destroy`. Enable `sanitize_and_reuse` only after the complete
disconnect, cleanup, re-preparation, and permission verification path has been
proven in the deployment.

The purchase shape is not free-form JSON. PAS accepts the server-owned
`agentic-dedicated-mysql` profile and generates the `CreateDBCluster` request.
Revision 1 fixes PolarDB MySQL `DBMinorVersion=8.0.2`, AgileServerless compute,
the prepared `agentic` database/account, and related Agentic parameters. The
editable dimensions are region, zone, VPC, VSwitch, and a server-allowlisted
storage type.
The initial default and only supported storage type is `essdpl1`; the selector
allows future profile revisions to add physical storage types without exposing
arbitrary purchase fields.

The permission selector is **Agent MySQL default permissions**: it controls the
MySQL account PAS returns for an Agent allocation, regardless of whether the
caller is a sandbox or another Agent runtime.

After creation, administrators can edit the pool name, capacity and rate
limits, storage type, reclaim policy, cooldown, and health-check settings from
the pool detail page. Region, zone, VPC, and VSwitch can be changed from the
collapsed **Advanced network configuration** section. PAS trims surrounding
whitespace from these identifiers before saving or sending them to
`CreateDBCluster`.

Changing network placement requires an explicit risk confirmation and the
latest pool configuration revision. The change does not migrate existing
PolarDB clusters, so the pool can temporarily contain members in both the old
and new networks. Administrators must confirm that PAS and routed Agents can
reach both placements. Members that have not started a cloud purchase,
including failed members whose progress is still `PURCHASE_INTENT_STORED`, use
the latest saved placement when retried. A member at `PURCHASE_REQUESTED` or a
later step keeps the placement already sent to Alibaba Cloud; an in-flight
request can therefore complete with the previous configuration.

The PAS private source CIDR setting is sent when PAS purchases a new member.
Changing it affects future members only; it does not reconcile the whitelist
of existing PolarDB clusters.

Permission-template revisions are immutable. To change permissions, create a
new revision from the pool's permission panel, then choose whether it becomes
the default for future Agent accounts. Existing accounts are changed only by a
separate preview-and-apply action. If the pool has no existing Agent accounts,
the console explains that no synchronization is required.

## Runtime readiness and simulation

The first wizard step shows user-actionable checks for the auto-provisioning
worker heartbeat, Alibaba Cloud purchase credentials, and the default Agent
MySQL permission revision. Missing
Alibaba Cloud access produces `ALIYUN_ACCESS_NOT_CONFIGURED`; a missing recent
heartbeat produces `DEDICATED_WORKER_NOT_RUNNING`. The pool may still be saved,
but it remains **Not started** and does not generate instances until every
blocking reason is resolved. This explains the common local-development case
where pool metadata exists but no AccessKey, AssumeRole, or ECS RAM role is
configured.

After online activation there can be a short interval before the first durable
heartbeat is written. During that interval readiness remains
`DEDICATED_WORKER_NOT_RUNNING`; it does not require a PAS restart.

The worker row explains that the PAS background worker purchases, initializes,
health-checks, recycles, and deletes pool-owned instances. When blocked, its
action opens `/settings/configuration?module=runtime_policy`. The credential
action opens `/settings/configuration?module=aliyun_access`. A ready row does
not show a redundant action.

The server-owned purchase profile remains an internal integrity check, not a
user-editable checklist item. `PURCHASE_PROFILE_INVALID` means the PAS version
or installation must be upgraded or repaired. `PERMISSION_TEMPLATE_UNAVAILABLE`
means the installation or metadata migration is incomplete; neither error is
fixed by entering arbitrary purchase parameters in the wizard.

`dedicated_pool_simulation_enabled` is an explicit development/test fallback.
PAS uses it only when no active Alibaba Cloud credential exists. An active
AccessKey, AssumeRole, or ECS RAM role always takes precedence, and the console
reports the effective real mode rather than showing a simulation warning.
When the fallback is disabled, missing cloud credentials fail closed. Never
leave this fallback enabled in a production deployment or treat simulated
readiness as proof that PolarDB purchase permissions work.

`dedicated_pool_preparation_mode` defaults to `full`. In `full` mode, a member
becomes `AVAILABLE` only after PAS connects to the private PolarDB endpoint,
applies the Agent permission template, and verifies both grants and `SELECT 1`.
Use the explicit `openapi_only` mode for local development when the workstation
cannot route to that private endpoint. PAS still creates real billable
clusters, lifecycle and Agent accounts, and the database, then persists
`OPENAPI_READY` and pauses. The member remains `REPLENISHING`, is not
allocatable, and returns no credentials.

Do not infer this mode from localhost, Docker, or host names. After deploying
PAS to a VPC that can reach the saved private endpoint, change the active
service runtime policy back to `full`; the in-process worker resumes the same
member at `OPENAPI_READY` without purchasing another cluster. A complete real
E2E test therefore runs PAS in the target VPC or a connected private network.
PAS never creates a public endpoint or broadens a whitelist automatically.

An administrator can manually retry a `REPLENISHING` member to clear its
scheduled backoff and wake the in-process worker immediately. The retry resumes
from the member's durable preparation step and never starts a second purchase.
At `OPENAPI_READY` in `openapi_only` mode, retry is intentionally unavailable:
that member has already reached the configured local-validation boundary.
Switch to `full` from a PAS deployment with private connectivity, then retry if
an earlier failure or backoff still delays continuation.

## Lifecycle administrator credentials

For PAS-purchased members, use `pas_managed`. PAS creates a lifecycle
administrator, encrypts its username and password, and uses it only for
database/account preparation, permission changes, connection termination,
verification, and cleanup. It is never returned to an Agent.

PolarDB `CreateAccount` can return before a new account is queryable. PAS waits
for both the lifecycle administrator and the Agent account to become
`Available` through `DescribeAccounts` before using either account. Database
creation associates no account: the PolarDB privileged lifecycle account
already has access to all databases, while `CreateDatabase` accepts only a
standard account assignment. PAS then applies the selected permission revision
to the Agent account through the data plane.

For externally registered members of an auto-provisioning pool, use `admin_provided`
and select an active `provisioning_admin` credential with capability `admin`
for that instance. PAS does not create that administrator. If the external
service uses PolarDB multitenant management, the account must also satisfy its
high-privilege requirements, including the `rds_kill_user_list` prerequisite.

An ordinary registered single-tenant instance assigned directly to an Agent
is not an automatically supplied auto-provisioning pool member and does not require a
lifecycle administrator. It remains under administrator allocation and direct
credential management.

## Preparation and allocation

A new member progresses through purchase intent, cluster creation, endpoint
resolution, lifecycle-account availability, Agent database/account
creation, permission application, and verification. PAS pre-creates the
database and Agent-facing account before the member becomes `AVAILABLE`.

When an Alibaba Cloud operation fails, the member row retains the bounded
request ID, failure time, OpenAPI operation, safe cloud error detail, and a
stable PAS failure code. Hover over the request ID to view these diagnostics.
Credential-like cloud messages are discarded rather than persisted or shown.

Only an `AVAILABLE` member with `FRESH` evidence newer than the configured
maximum age is allocatable. A hot create atomically reserves the member and
returns `READY` credentials without a PolarDB OpenAPI purchase or initialization
SQL in the request path. If no fresh member exists, PAS first records an
`ALLOCATED_PREPARING` member inside the selected pool, pins it to the requesting
resource, and only then starts the cloud purchase under the same hard limits.
The request returns `CREATING`. A cold-created cluster never exists outside
pool accounting or changes pools after purchase intent is durable.

Pool capacity fields mean:

- `allocatable`: fresh, verified members available now.
- `planning`: unallocated `AVAILABLE` members plus unallocated members already
  `REPLENISHING`.
- `billable_total`: every non-`DELETED` member, including allocated,
  `DELETING`, and cooling resources.
- `surplus`: planning capacity above `target_size`; the console warns only
  when this value is greater than zero.

Cooling members do not count toward `planning`, so PAS may replenish the hot
minimum, but they continue to count toward `billable_total`. This prevents a
create/delete loop from bypassing `max_total_members` while old clusters are
still billable.

The console reports configured pool status separately from derived supply
state. Supply states are `NOT_STARTED`, `PREWARMING`, `PARTIALLY_READY`,
`READY`, `CAPACITY_LIMITED`, and `ERROR`, accompanied by exact `N/M` fresh-ready
capacity and every current blocking code. `POOL_CAPACITY_LIMIT_REACHED` means
the billable hard limit prevents further replenishment; cooling members remain
part of that billable count.

## Agent primary and fallback routing

A pool can prewarm without an Agent binding, but no Agent can consume it until
an administrator adds it to that Agent's auto-provisioning pool route list. Each Agent has
one enabled **primary** pool at `routing_order=0` and zero or more ordered
**fallback** pools. There is no global pool priority. The Agent detail page can
append a fallback, replace the primary after confirmation, reorder fallbacks,
pause a route, unlink it, or open the pool.

Selection walks fresh hot capacity in `routing_order`. When no hot member is
available, cold purchase starts from the primary; a fallback cold purchase is
used only for a deterministic pre-purchase blocker. A timeout or transient
cloud failure after purchase intent does not jump to another pool, preventing
duplicate billable clusters. Reordering affects future creates only. Existing
resources remain pinned to their original pool for delete, cooldown, restore,
and cleanup. A route with nonterminal resources must be paused and cannot be
unlinked until those resources finish.

## Readiness and failure handling

PAS periodically rechecks `AVAILABLE` members. Readiness evidence uses
`FRESH`, `STALE`, and `CHECKING`. Expired evidence changes `FRESH` to `STALE`,
excludes the member from allocation, and forces a recheck.
Staleness alone does not quarantine a member or purchase a replacement; this
prevents a health-worker outage from replacing an otherwise healthy pool.

A successful recheck returns the member to `FRESH`. An inconclusive internal
or connectivity failure leaves it stale and schedules another recheck. Only a
conclusive failed verification moves the member to `QUARANTINED`; replacement
then remains subject to the hard member and purchase budgets. Administrators
may retry, explicitly quarantine, or destroy an unallocated member from the
pool page.

Use `POOL_CAPACITY_LIMIT_REACHED` to distinguish the hard billable ceiling and
`RATE_LIMITED` for a purchase or Agent operation budget. Alert on sustained
planning deficit, replenishment velocity, repeated failures, stale/checking
growth, quarantined growth, and permission verification failure.

## Permission templates and synchronization

Each pool pins an immutable permission-template revision. The default template
contains common database-scoped DDL/DML privileges and excludes `CREATE USER`;
`grant_option` is false. Administrators may create a new revision with an
explicit subset of supported privileges. `CREATE USER` and `grant_option` are
powerful Dedicated-only choices and are never enabled by default.

Changing the pool's selected revision affects subsequent preparation. To
change existing members or one allocated resource, open the permission drawer,
run `dry_run`, review every target and previous revision, then explicitly
confirm `apply`. PAS stores a durable job, revokes old effective grants,
applies the exact snapshot, and verifies the result. A failed target retains a
sanitized failure code and can be diagnosed without exposing credentials.

## Legacy purchase-profile upgrade

Existing pools with empty, incomplete, or conflicting purchase JSON report
`PURCHASE_PROFILE_UPGRADE_REQUIRED` and cannot purchase new capacity. The pool
detail page shows the typed replacement, including `DBMinorVersion=8.0.2` and
`essdpl1`, and requires an explicit revision-checked confirmation. Upgrade
preserves network, capacity, permission, cooldown, member, and Agent binding
data; it never silently interprets or reuses arbitrary legacy JSON.

## Retired pool compatibility guard

The former `resource_pool` configuration module, `/api/pool` and `/api/quota`
routers, human auto-purchase path, retry-provision endpoints, and legacy
startup recovery sweep are retired. PAS does not adopt, delete, or silently
convert old physical rows.

Before auto-provisioning purchasing, PAS audits retained metadata. A nonzero legacy
target reports `LEGACY_POOL_CONFIG_PRESENT`; an old pooled physical row reports
`LEGACY_POOL_INSTANCE_PRESENT`. Either code blocks new purchases until an
administrator backs up the metadata and explicitly resolves the obsolete
state. A zero-target legacy document alone does not create a new pool or buy a
cluster.

## Delete, cooldown, and restore

Delete first revokes Agent access, then uses the lifecycle administrator to
terminate the sandbox database account's existing MySQL sessions. PAS sets
`disconnected_at` only after verification; `cooldown_until` equals that time
plus the effective duration. Cleanup cannot start merely because a deadline
passed while disconnection remains unverified.

The duration inheritance order is member/resource, pool, then global
`delete_cooldown_duration_hours`. The global default is 24 hours. Values are
whole hours with an intentional minimum of 1; tests and operational drills use
an injected clock instead of weakening the production floor.

Before irreversible cleanup, an administrator may restore from `DELETING`,
`DELETE_FAILED`, or `COOLING_DOWN`. PAS reconnects and verifies the old
database/account, then reactivates the old credentials so an unchanged sandbox
can resume. A failed restore remains in the recoverable source state with its
deadline intact and records a sanitized reason. Restore is unavailable after
logical cleanup, sanitize dispatch, physical destruction, or completion.

At expiry, `destroy` deletes the physical member. `sanitize_and_reuse` drops
the sandbox database/account, clears its credential material and permission
snapshot, and returns the member to preparation. Both paths release active
capacity exactly once.

## Drain and operate safely

Draining sets `target_size=0` and stops new allocation through the pool while
allowing existing resource disconnect, cooldown, restore, permission sync, and
cleanup to finish. Do not delete metadata or revoke the lifecycle administrator
until every associated member and resource reaches a safe terminal state.

Back up the metadata database and `PAS_ENCRYPTION_KEY` as one recovery set.
The database contains encrypted lifecycle and sandbox credentials; either part
alone is insufficient. Audit records cover pool changes, member actions,
template revisions, synchronization, and resource restore.

For the Agent contract, see [Agent REST database provisioning](agent-rest-provisioning.md).
For migration and mixed-version boundaries, see
[Upgrade and rollback](../deployment/upgrade-and-rollback.md).
