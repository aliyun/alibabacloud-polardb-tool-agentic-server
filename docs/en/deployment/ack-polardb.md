# ACK with PolarDB for MySQL

[简体中文](../../zh-cn/deployment/ack-polardb.md)

For an Alibaba Cloud ACK production deployment, use an existing PolarDB for
MySQL 8.0 cluster as the PAS metadata database. Do not deploy the Compose
MySQL container in Kubernetes.

## Database preparation

Create a dedicated database and least-privilege account for PAS metadata. The
account must be allowed to create and alter the tables, indexes, and migration
metadata managed by Alembic. Back up the cluster and root encryption key
before each upgrade.

Use the cluster endpoint reachable from every ACK node/Pod:

```text
mysql+asyncmy://USER:PASSWORD@POLARDB-ENDPOINT:3306/DATABASE
```

Percent-encode reserved characters in the username and password. Put the
complete URL only in the existing Kubernetes Secret.

## Placement

Prefer ACK nodes and the PolarDB cluster in the same region and VPC. Configure
vSwitch routes, security groups, and the PolarDB IP whitelist for the actual
Pod source addresses. If the cluster is across VPCs, establish CEN or another
approved private route before installation.

After creating the Secret, install the Helm Chart as described in the
Kubernetes guide. The migration Hook proves database access before application
Pods are rolled out.
