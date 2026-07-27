# Production networking

[简体中文](../../zh-cn/deployment/networking.md)

All connectivity checks and SQL forwarding originate from the PAS backend Pod,
not the administrator's browser. Every replica therefore needs equivalent DNS,
routing, security-group, and database-whitelist access.

## Alibaba Cloud OpenAPI

Configure `aliyun_access.openapi_network` according to Pod connectivity:

- `public`: `polardb.<region>.aliyuncs.com` and
  `sts.<region>.aliyuncs.com`.
- `vpc`: `polardb-vpc.<region>.aliyuncs.com` and
  `sts-vpc.<region>.aliyuncs.com`.

AssumeRole needs both STS and PolarDB connectivity. In a VPC-only environment,
verify CoreDNS can resolve the VPC endpoints through Alibaba Cloud DNS or
PrivateZone, and verify routes and security policy allow HTTPS port `443`.
Custom endpoint hostnames are not accepted.

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
