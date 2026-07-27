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

## Redaction

Never log AccessKeys, passwords, Agent Tokens, bootstrap tokens, cookies,
ciphertext, SQL parameter values, or full secret-bearing exceptions. Use
stable error codes and request identifiers to correlate events.

## Retention

Set log directory, rotation size, backup count, timezone, and audit retention
through the active observability/security configuration. Size ephemeral
volumes for the selected policy or ship logs before Pod replacement.
