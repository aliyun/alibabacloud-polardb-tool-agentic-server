# Logs and observability

[简体中文](../../zh-cn/operations/logs-and-observability.md)

PAS writes structured process logs to stdout and can maintain rotated
persistent logs under `/app/log`. Container platforms should collect stdout
with Pod/container identity.

## Startup and migration

Keep migration executor logs separately from application logs, whether the
executor is a Compose container, Helm Job, or managed one-shot Pod. Startup
records schema-gate status, guided-setup readiness, and sanitized configuration
reload results. The first bootstrap token may appear once in stdout, so
restrict initial log access and retention.

## Runtime signals

Monitor HTTP status and latency, MCP tool outcomes, SQL policy blocks,
authentication failures, configuration version lag, database pool pressure,
provisioning queue age, lifecycle failures, and audit retention jobs. Metrics
must avoid high-cardinality SQL, token, account, or credential labels.

Dedicated signals include allocation hot hit/cold miss and duration;
allocatable, planning, billable, surplus, stale, checking, and quarantined
capacity; purchase reservation and replenishment velocity; rate/capacity guard
rejection; readiness outcomes; delete/disconnect/cooldown/restore/cleanup
outcomes; permission synchronization; and omitted MCP provisioning mode.
Labels use bounded signal/outcome/kind values. Pool names, member IDs, Agent
names, database names, usernames, endpoints, tokens, and raw errors are not
metric labels.
PolarRAG Tool governance emits `polarrag_tool_rejections_total` with `tool`
and sanitized internal `reason` labels, plus
`polarrag_tool_instance_inflight` with a 12-character hashed `instance_scope`.
Alert on sustained rejection growth and in-flight values near the configured
ceiling. Never treat `instance_scope` as a reversible instance identifier.

Idle full user and Agent rate buckets are removed periodically, and the
process-local bucket table has a hard capacity. This bounds memory under
identity churn while preserving the documented per-replica, approximate
limiting model. The governor removes only buckets that have fully refilled;
when active buckets occupy the capacity, a new identity fails closed with the
normal rate-limit response instead of resetting an active identity's budget.

Every authenticated PolarRAG Tool execution that reaches governance uses the
existing `polarrag.<tool>` audit action. Governance rejection records include
only the stable error, public reason, retry interval, requested fanout, and
current in-flight count when applicable; caller-supplied resource IDs, user
IDs, Agent IDs, raw instance IDs, ACL context, and upstream details are not
added to the rejection audit payload. A `429` must correspond to zero new
upstream calls for that rejected request.

## Redaction

Never log AccessKeys, passwords, Agent Tokens, bootstrap tokens, cookies,
ciphertext, SQL parameter values, or full secret-bearing exceptions. Use
stable error codes and request identifiers to correlate events.

## Retention

Set log directory, rotation size, backup count, timezone, and audit retention
through the active observability/security configuration. Size ephemeral
volumes for the selected policy or ship logs before Pod replacement.

## Dedicated alert interpretation

A `stale_excluded` readiness signal is not a purchase or member failure. Alert
on its sustained growth and worker lag, but count only
`replenishment/purchase_reserved` when evaluating purchase velocity. Compare
`billable_total` with `max_total_members`; cooling and deleting members remain
billable even though they do not satisfy planning capacity.
