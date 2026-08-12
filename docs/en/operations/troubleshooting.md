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

## Alibaba Cloud credential errors

Record the safe Alibaba Cloud Request ID shown with the result, then correct
the named boundary without copying a credential or raw SDK message into a
ticket. The following stable codes apply to `aliyun_access` validation and
runtime failures:

| Code | Corrective action |
| --- | --- |
| `OPENAPI_STS_SOURCE_CREDENTIAL_INVALID` | Replace the PAS-stored AssumeRole source AccessKey through a secure input; check that it is enabled and belongs to the intended source identity. |
| `OPENAPI_STS_ASSUME_ROLE_DENIED` | Grant the source identity only `sts:AssumeRole` on the configured target Role ARN. |
| `OPENAPI_STS_ROLE_TRUST_REJECTED` | Correct the target role trust policy so it allows the intended source principal, rather than broadening the source policy. |
| `OPENAPI_STS_EXTERNAL_ID_MISMATCH` | Make the configured External ID exactly match the condition required by the target trust policy; do not log or display its value. |
| `OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED` | Attach the intended RAM role to the ECS instance running the affected PAS Pod, then verify every replica. |
| `OPENAPI_ECS_METADATA_DISABLED` | Enable the instance metadata service access required by the deployment policy and retry from the backend Pod. |
| `OPENAPI_ECS_IMDSV2_UNAVAILABLE` | Restore IMDSv2 reachability from the Pod; PAS has no IMDSv1 fallback, metadata URL override, or HTTP proxy path. |
| `OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED` | Repair STS or IMDS connectivity and the active role configuration, then retry the failed cloud operation; PAS fails closed after expiration. |
| `OPENAPI_CREDENTIAL_INVALID` | Replace the active direct credential through the secure configuration workflow. |
| `OPENAPI_PERMISSION_DENIED` | Grant only the required PolarDB OpenAPI actions to the active target role or direct identity. |
| `OPENAPI_DNS_FAILURE`, `OPENAPI_TLS_FAILURE`, `OPENAPI_CONNECT_FAILURE` | Check DNS, route, HTTPS `443`, certificate trust, and the selected `public` or `vpc` endpoint from the PAS Pod. |
| `OPENAPI_ENDPOINT_UNSUPPORTED` | Correct the selected region or network value; custom endpoint hostnames are not supported. |

Do not use a retained credential, a different mode, or IMDSv1 as an automatic
workaround. Make an explicit, audited configuration change after identifying
the failing boundary.

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
