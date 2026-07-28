# Agent tool reference

[简体中文](../../zh-cn/agents/tool-reference.md)

PAS exposes tools dynamically. An Agent sees only tools allowed by its active
bindings and resource ownership.

## Instance inventory and provisioning

- `list_db_instances(cursor?, limit?, db_type?, source?, status?)` lists
  authorized bound instances and owned provisioned resources.
- `create_db_instance(client_token, db_type, name?)` creates a persistent
  resource when `db_instance:create` is granted. `db_type` is currently
  `polardb_mysql`; `client_token` is the required idempotency key.
- `describe_db_instance(db_instance_id)` describes one authorized identifier
  returned by the list tool. Credentials appear only with credential-read
  authorization and a ready resource.
- `delete_db_instance(db_instance_id)` deletes only an owned provisioned
  resource, not a registered physical instance.

Use `has_more` and `next_cursor` for pagination. An invalid cursor returns
`INVALID_CURSOR`; it never silently restarts at page one.

## SQL and schema

- `run_sql(sql, instance_id, database?, branch?, max_rows?, cursor?, confirm?)`
  executes one statement.
- `run_sql_transaction(sql_statements, instance_id, database?, confirm?)`
  executes a list in one transaction subject to MySQL implicit-commit rules.
- `describe_schema(instance_id, database?, table_pattern?, include_columns?,
  cursor?, max_tables?)` returns tables, comments, and optional columns.

For an Agent, `instance_id` is required and must exactly match a
`db_instance_id` returned by `list_db_instances`. Display names and cluster
IDs are not substitutes. `sql:read` permits read-only operations;
`sql:write` additionally requires a `readwrite` binding and backend privilege.

## User-oriented tools

Human-user sessions may also expose `set_default_instance`, `list_branches`,
`create_branch`, and `delete_branch` when their runtime access supports those
operations. Agents should not assume these tools exist.

## Actionable errors

When a tool returns `INSTANCE_NOT_ACCESSIBLE`, call `list_db_instances` and use
one returned identifier. `DB_INSTANCE_NOT_FOUND` means the identifier is
unknown or no longer visible. `NO_PROVISIONING_BACKEND` and
`CAPACITY_EXHAUSTED` require an administrator to review provisioning bindings
or capacity. `DATABASE_REQUIRED` means the direct binding has no default
database: run `SHOW DATABASES`, choose an authorized database, and retry with
the `database` argument. `RATE_LIMITED` requires bounded retry. Never guess
another tenant's identifier or credentials.
