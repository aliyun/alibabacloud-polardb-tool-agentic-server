# Multitenant provisioning

[简体中文](../../zh-cn/database-instances/multitenant-provisioning.md)

A registered `multitenant` PolarDB MySQL instance can become a provisioning
backend for departments or explicitly authorized Agents.

## Prerequisites

The registration preflight requires connectivity, `enable_multi_tenant=ON`,
and a provisioning administrator listed by `rds_kill_user_list`. The credential
needs the PolarDB tenant/resource-control privileges required by the backend.
Use the official PolarDB multitenant documentation for cluster enablement.

Direct access remains a separate purpose. A multitenant instance may have a
`direct_access` credential for existing databases and a
`provisioning_admin` credential for lifecycle DDL; do not reuse the
administrator credential for ordinary SQL.

## Backend policy

Configure capacity, CPU range, DDL concurrency, priority, and lifecycle state.
`active` accepts placement, `draining` blocks new placement while preserving
cleanup, and `disabled` reserves the backend for recovery. Capacity controls
placement but never hides an Agent's existing owned resources.

## Agent provisioning

Grant **Create managed databases** on an active healthy backend. The Agent then
receives `create_db_instance`, while direct SQL capability remains optional.
Creation automatically establishes a database account and stores encrypted
connection data. `describe_db_instance` returns it only to the owning,
authorized Agent after status reaches `READY`.

## Lifecycle and recovery

Creation and deletion are asynchronous and idempotent. Observe `CREATING`,
`READY`, `CREATE_FAILED`, `DELETING`, `DELETED`, and `DELETE_FAILED`.
`client_token` remains permanently associated with its normalized request.
Retry failed cleanup through the administrator workflow after diagnosing the
sanitized failure and backend state.
