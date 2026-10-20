# Production networking

[简体中文](../../zh-cn/deployment/networking.md)

All connectivity checks and SQL forwarding originate from the PAS backend Pod,
not the administrator's browser. Every replica therefore needs equivalent DNS,
routing, security-group, and database-whitelist access.

## Alibaba Cloud OpenAPI

Configure `aliyun_access.openapi_network` according to Pod connectivity:

- `public`: the resolver uses the global `polardb.aliyuncs.com` hostname for
  central regions (`cn-hangzhou`, `cn-shanghai`, `cn-beijing`,
  `cn-wulanchabu`, `cn-heyuan`, `cn-hangzhou-finance`, and
  `cn-beijing-finance-1`); other regions use
  `polardb.<region>.aliyuncs.com`. STS remains regional at
  `sts.<region>.aliyuncs.com`.
- `vpc`: `polardb-vpc.<region>.aliyuncs.com` and
  `sts-vpc.<region>.aliyuncs.com`.

AssumeRole needs both STS and PolarDB connectivity. In a VPC-only environment,
verify CoreDNS can resolve the VPC endpoints through Alibaba Cloud DNS or
PrivateZone, and verify routes and security policy allow HTTPS port `443`.
Custom endpoint hostnames are not accepted.

PAS uses an offline, reviewed catalog from the official
[PolarDB endpoints](https://api.aliyun.com/product/polardb?tab=endpoints) and
[STS endpoints](https://help.aliyun.com/zh/ram/developer-reference/api-sts-2015-04-01-endpoint)
tables (reviewed 2026-09-24). The names above describe supported entries, not
a rule for inventing hostnames. Unknown regions or missing endpoints fail with
`OPENAPI_ENDPOINT_UNSUPPORTED`; VPC never falls back to public. In particular,
PolarDB VPC is unavailable in `cn-beijing-finance-1`, and STS VPC is unavailable
in `cn-hangzhou-finance`. Direct AccessKey mode does not require STS.

`aliyun_access.region_id` is the PAS access/default region for preflight and
STS. In VPC mode it must be the PAS deployment region, and all OpenAPI-managed
resources must be in that same region. Cross-region VPC connectivity does not
make regional VPC OpenAPI endpoints cross-region. Public mode can manage
multiple regions, using each pool or instance's own region for PolarDB calls.
Changing the global default never rewrites existing resource regions.

Failed preflights include the resolved service, region, network, endpoint,
stage, and a sanitized error code/request ID when available. Check these fields
before testing DNS, routing, port 443, or credentials from the backend Pod.
`OPENAPI_DNS_FAILURE`, `OPENAPI_TLS_FAILURE`, and `OPENAPI_CONNECT_FAILURE`
also classify typed transport errors retained inside Alibaba Cloud SDK retry
wrappers. Raw SDK messages and credentials are not returned to the browser.
`OPENAPI_CHECK_FAILED` means an unclassified check failure, not proof of a
network outage. No raw SDK exception or credential is returned.

## AssumeRole and ECS requirements

For `assume_role`, create a dedicated source identity for PAS. Its source
policy grants only `sts:AssumeRole` on the one target role that PAS uses; do
not grant a wildcard resource or PolarDB permissions to that source identity:

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "acs:ram::<account-id>:role/pas-runtime"
    }
  ]
}
```

On the target role, configure a trust policy that permits only the intended
source principal to assume the role. If PAS supplies an External ID, require
the same value in the target trust policy condition. Attach the required
PolarDB OpenAPI permissions to this target role, not to the source identity.
Keep the source AccessKey only in PAS; STS temporary credentials are never
persisted.

For `ecs_ram_role`, attach the target RAM role to every ECS instance running a
PAS backend Pod. PAS calls the fixed ECS metadata service through the official
SDK with IMDSv2 only. Allow the Pod-to-metadata path required by the instance,
but keep no HTTP proxy for metadata, do not expose a metadata URL setting, or
expect an IMDSv1 fallback. Verify the role binding and IMDSv2 access from each
replica before enabling the mode.

## Database endpoints

Registered instance **Test Connection**, credential tests, provisioning DDL,
and Agent SQL-over-HTTP requests all use the selected PAS Pod's network path.
Allow MySQL port `3306` from every possible Pod source address and keep
whitelists synchronized during node-pool or VPC changes. Use CEN or approved
private networking for cross-VPC instances.

## Ingress and TLS

The Chart creates an Ingress only when enabled. The operator owns the Ingress
controller, public or private load balancer, DNS record, TLS certificate,
request-size/time-out policy, and source restrictions. Terminate TLS only at
an approved boundary and set the configured external base URL to the actual
HTTPS address before enabling SSO.
