# Register database instances

[简体中文](../../zh-cn/database-instances/registration.md)

Physical database registration is centralized under **Instances**. Departments,
users, and Agents select an existing registration instead of entering another
connection tuple.

## Required fields

Register the cluster ID, display name, engine, topology, optional region and
usage, plus host, port, username, and password. PolarDB for MySQL uses port
`3306` by default. Allocation mode is fixed to `registered` and is not shown
as a choice.

The usage text is returned by `list_db_instances` and
`describe_db_instance`, helping an Agent select the correct workload without
changing the stable instance UUID.

## Test before save

**Test Connection** originates from the PAS backend Pod and executes
`SELECT 1` with the complete host, port, username, and password. The result
remains visible below the button. Changing any connection field invalidates
the previous result and requires another test.

For `multitenant`, registration additionally checks
`enable_multi_tenant=ON` and verifies the configured username appears in
`rds_kill_user_list`. A connection can succeed while these provisioning
preconditions fail.

## Edit and rotate

The detail page can edit display name, usage, region, host, and port. Changing
host or port requires a new connection test. Cluster ID, engine, topology, and
allocation mode are immutable identity fields.

Credentials are managed separately. Add, test, edit, or revoke a credential
to rotate a backend password without replacing the instance registration.
Existing bindings continue to reference the credential record.

## Permission boundary

MCP can access only databases allowed by the selected MySQL account. PAS does
not bypass or elevate MySQL grants. Use separate least-privilege credentials
for direct access and provisioning administration.
