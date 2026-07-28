# Resource requirements

**English** | [简体中文](../../zh-cn/getting-started/cloud-resources.md)

This page prepares two cloud resources: the ECS that runs PAS, and the PolarDB
MySQL cluster used as the PAS metadata database. When done, you will have the
connection details required by the deployment stage.

## What you will get

- A publicly reachable ECS to run PAS deployed with Docker Compose.
- A PolarDB MySQL 8.0 cluster as the PAS metadata database (it stores metadata
  such as configuration, credentials, and the key ring — not the target
  business database an Agent uses).
- A usable `PAS_DATABASE_URL` connection string.

## Step 1: Buy an ECS instance

Create an instance in the
[ECS console](https://ecs.console.aliyun.com/home). Recommended:

- Region: the same region and zone as the PolarDB metadata database for
  private connectivity.
- Size: 2 vCPU / 4 GiB is enough to start for a trial.
- System disk: a cloud disk of 40 GiB or more.
- Image: a mainstream Linux distribution (such as Alibaba Cloud Linux /
  Ubuntu).
- Public network: assign a public IP or bind an EIP to reach the console
  during the trial.
- Security group: allow inbound SSH (TCP 22) and the PAS port (TCP 18760),
  ideally restricted to your office network range.

## Step 2: Buy PolarDB MySQL as the metadata database

Create a PolarDB MySQL 8.0 cluster in the
[PolarDB console](https://polardb.console.aliyun.com/overview) and complete:

- Keep the region and VPC the same as the ECS bought in step 1.
- Recommended specification: Enterprise Edition 2C8G
  (`polar.mysql.x4.medium`); upgrade later if capacity requires.

<p align="center">
  <img src="../../zh-cn/getting-started/images/polardb-purchase.png" alt="PolarDB purchase page settings" width="820">
</p>

- Create a database account (username + password), for example `pas`.
- Create an empty database (for example `pas_meta`) for PAS metadata, and
  grant the account access to that database.

<p align="center">
  <img src="../../zh-cn/getting-started/images/polardb-create-database.png" alt="Create the database and grant the account" width="820">
</p>

- Record the cluster endpoint and port (3306 by default).

## Step 3: Connect network and account

- If the ECS and PolarDB share a VPC, prefer the private endpoint.
- Add the ECS private/public address to the PolarDB access whitelist.
- Verify connectivity from the ECS with a MySQL client and confirm the account
  can log in to the target database.

<p align="center">
  <img src="../../zh-cn/getting-started/images/ecs-mysql-connect.png" alt="Verify connectivity from the ECS" width="820">
</p>

## Output checklist

Before moving to deployment, confirm you have:

- The ECS public address and SSH login method.
- The PolarDB endpoint, port, account, password, and database name.
- The connection string built from them (note: special characters in the
  password must be URL-encoded, for example `@` becomes `%40`):

```text
mysql+asyncmy://USER:PASSWORD@ENDPOINT:3306/DATABASE
```

Next: [Deployment (single ECS + Docker Compose)](./deploy-compose.md).
