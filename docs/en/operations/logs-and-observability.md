# Logs and observability

[简体中文](../../zh-cn/operations/logs-and-observability.md)

PAS writes structured process logs to stdout and can maintain rotated
persistent logs under `/app/log`. Container platforms should collect stdout
with Pod/container identity.

## Startup and migration

Keep migration Job logs separately from application logs. Startup records
schema-gate status, guided-setup readiness, and sanitized configuration reload
results. The first bootstrap token may appear once in stdout, so restrict
initial log access and retention.

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
