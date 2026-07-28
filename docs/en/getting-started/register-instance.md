# Feature usage 2: register a database instance

**English** | [简体中文](../../zh-cn/getting-started/register-instance.md)

Besides instances created automatically by the resource pool, you can also
register an existing PolarDB MySQL cluster and then authorize it for Agents.
This page walks through one manual registration.

## Open the instance list

Signed in as an administrator, open the **Instances** page. The list shows
each instance's engine, topology, allocation mode, status, and provisioning
capability; **Register Instance** in the top-right corner registers a new
instance.

<p align="center">
  <img src="../../zh-cn/getting-started/images/instances-page.png" alt="Instances list page" width="820">
</p>

## Fill in the registration details

Click **Register Instance** and fill in the target cluster's connection
details:

- **Cluster ID**: the PolarDB cluster ID, such as `pc-xxxxxxxx`.
- **Name**: the display name inside the console.
- **Usage**: a description for later identification (optional).
- **Engine** / **Topology**: choose `PolarDB for MySQL` and `Single tenant`.
- **Region** / **Port**: the cluster region and port (3306 by default).
- **Host**: the cluster connection endpoint.
- **Username** / **Password**: the database account used to access this
  instance.

This account is the identity MCP uses to forward SQL. Database permissions
are enforced entirely by the MySQL backend; the service does not bypass or
elevate them, so prepare the account with least privilege.

## Test the connection and save

Click **Test Connection**, wait for **Connection succeeded**, then click
**Save Instance**. The connection test is initiated by the service replica
handling the request, so it verifies both network connectivity from the
service to the target cluster and the account validity.

<p align="center">
  <img src="../../zh-cn/getting-started/images/register-instance-form.png" alt="Register Instance form with connection test" width="820">
</p>

If the test fails, first check that the cluster whitelist allows the address
of the ECS running the service, that the host and port are correct, and that
the account and password work.

## Status after registration

After saving, the instance appears in the list with the `registered`
allocation mode; once its status is `active` it can be authorized. Only
instances in `active` or `stopped` status can be bound to Agents.

## Learn more

For the full explanation of connection details, credential management,
multitenant preflight, and rotation, see
[Registration](../database-instances/registration.md) and
[Database instance access and provisioning](../database-instances/access-and-provisioning.md).

Next: [Feature usage 3: Agent, Token, and MCP](./agents-and-mcp.md).
