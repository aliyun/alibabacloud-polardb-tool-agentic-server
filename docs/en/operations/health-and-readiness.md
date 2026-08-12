# Health and readiness

[简体中文](../../zh-cn/operations/health-and-readiness.md)

Kubernetes and external monitors should distinguish process liveness from
traffic readiness.

## Endpoints

`GET /livez` confirms the process event loop is alive. It does not prove
database, configuration, OpenAPI, or registered-instance connectivity.

`GET /readyz` compares the Pod's loaded configuration version with the shared
metadata database. It returns `503` when the database version cannot be read,
the Pod is stale, or a required reload failed. Successful setup-mode readiness
is still `200`; the response `mode` identifies `SETUP` or `READY`.

`GET /healthz/dependencies` is a limited dependency summary. Use explicit
connection and OpenAPI validation workflows for external services.

## Multi-replica behavior

Configuration writes increment one shared version. Every Pod polls, atomically
swaps its runtime snapshot, and becomes ready only when its loaded version is
current. The default interval is five seconds. Optional module reload failures
are reported separately from required failures.

## Dedicated member readiness

The application readiness endpoint does not mean every hot-pool member is
allocatable. Dedicated members carry separate `FRESH`, `STALE`, or `CHECKING`
evidence. PAS allocates only `AVAILABLE` + `FRESH` members whose evidence age
is within the pool maximum.

Expired evidence excludes a member and schedules a recheck. It does not by
itself quarantine the member or trigger a replacement purchase. Only a
conclusive failed recheck moves it to `QUARANTINED`; inconclusive worker or
network failure stays stale and retries. The maximum evidence age must be at
least twice the check interval.

## Alerting

Alert on sustained readiness failure, restart loops, migration Job failure,
database connection exhaustion, provisioning failures, and repeated
authentication rejection. For auto-provisioning pools also alert on sustained
planning deficit, stale/checking or quarantined growth, failed verification,
hard-member exhaustion, and replenishment velocity. Capture response codes and
sanitized categories, not secrets or full connection strings.
