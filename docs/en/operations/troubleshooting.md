# Troubleshooting

[简体中文](../../zh-cn/operations/troubleshooting.md)

Start with the narrowest failed boundary and retain sanitized evidence.

## Service does not start

Run `pas database check`. Resolve `DATABASE_SCHEMA_NOT_INITIALIZED`,
`DATABASE_SCHEMA_OUTDATED`, `DATABASE_SCHEMA_TOO_NEW`,
`DATABASE_MIGRATION_HEAD_INVALID`, or `DATABASE_UNAVAILABLE` before restarting.
Do not bypass the gate or allow every replica to migrate.

For decryption failures, confirm every Pod uses the same original
`PAS_ENCRYPTION_KEY`. Do not experiment with replacement keys against the
production database.

## Pod is not ready

Inspect `/readyz`. Compare `desired_config_version` and
`loaded_config_version`, then inspect `last_reload_error` and module errors.
Verify database latency and that the poll interval has elapsed. A stale Pod
correctly receives no Service traffic.

## External validation fails

DNS, route, TLS, credential, and permission failures have separate sanitized
codes. In VPC mode test resolution of regional `polardb-vpc` and `sts-vpc`
endpoints from the backend Pod. Instance Test Connection also runs from that
Pod; check MySQL whitelist, security groups, host, port, username, and password.

## MCP or SQL fails

Reconnect after binding changes. Call `list_db_instances`, use the returned
`db_instance_id` as `instance_id`, and confirm the binding exposes required SQL
capability. Then verify the stored MySQL account grants the requested database
and statement. Do not broaden privileges before identifying which layer
rejected the request.

## Provisioning is stuck

Inspect backend health, capacity, lifecycle state, worker ownership, and the
resource failure code. `enable_multi_tenant` must be on and the provisioning
administrator must pass preflight. Retry only through the supported recovery
action so idempotency and cleanup state remain intact.
