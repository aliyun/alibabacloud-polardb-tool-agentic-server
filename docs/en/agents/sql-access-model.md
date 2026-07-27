# Agent SQL access model

[简体中文](../../zh-cn/agents/sql-access-model.md)

Agent SQL-over-HTTP access is opt-in per direct instance binding. Selecting
`readwrite` permission alone does not expose SQL tools when SQL proxy
capability is disabled.

## Capability derivation

With SQL proxy enabled:

- `readonly` derives `sql:read`.
- `readwrite` derives `sql:read` and `sql:write`.

Metadata capabilities `db_instance:list`, `db_instance:describe`, and
`db_instance:credentials:read` are independent. The credential-read capability
reveals connection material through the audited describe workflow; it is not
required for PAS to proxy SQL with the stored credential.

## Resource coherence

An Agent can run SQL only on an active bound instance or its own ready
provisioned resource returned by `list_db_instances`. The required
`instance_id` prevents an LLM from falling back to an unrelated default.
Provisioned resources use automatically created credentials and become
queryable only after lifecycle status reaches `READY`.

## SQL policy

PAS classifies every statement, intersects capability and binding permission,
applies row and timeout limits, checks destructive confirmation, and records
an audit result. The selected MySQL account independently restricts databases,
tables, and statements. PAS cannot bypass those grants.

Transactions containing DDL follow MySQL implicit-commit semantics; they
cannot promise rollback of every statement. `DROP DATABASE` is blocked by the
default safety policy.

## Operational guidance

Grant a database-scoped MySQL account, test the credential from the backend,
enable only required capabilities, and reconnect the Agent client. On
authorization failure, list visible resources again instead of modifying the
identifier.
