# Deployment (single ECS + Docker Compose)

**English** | [简体中文](../../zh-cn/getting-started/deploy-compose.md)

This page deploys PAS on a single ECS with the PolarDB MySQL metadata database
prepared in [Resource requirements](./cloud-resources.md).

## Prerequisites

- SSH access to the ECS with sudo privileges.
- The PolarDB endpoint, database name, username, and password.
- The ECS security group already allows TCP 18760.

## Step 1: Install Docker

Install Docker Engine and the Compose plugin on the ECS:

```bash
curl -fsSL https://get.docker.com | sh
sudo docker version
sudo docker compose version
```

If Compose is unavailable, follow
[Install and use Docker on ECS](https://help.aliyun.com/zh/ecs/user-guide/install-and-use-docker)
to install the Compose plugin.

## Step 2: Download the deployment files

Download and extract the release to deploy:

```bash
PAS_VERSION=0.0.6
wget "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/archive/refs/tags/v${PAS_VERSION}.tar.gz"
tar -xzf "v${PAS_VERSION}.tar.gz"
cd "alibabacloud-polardb-tool-agentic-server-${PAS_VERSION}"
```

Run all later commands inside this directory.

## Step 3: Generate .env

Run the containerized helper and enter the metadata database connection:

```bash
./scripts/deploy/create-external-mysql-env.sh
```

<p align="center">
  <img src="../../zh-cn/getting-started/images/external-mysql-env-generator.png" alt="Generate the environment file and test the metadata database connection" width="820">
</p>

The helper displays non-secret fields for confirmation, masks password input
with `*`, and executes `SELECT 1` before writing `.env`. Enter `n` at
`Use these settings? [Y/n]` to correct the values.

When the helper runs through Docker, `127.0.0.1` and `localhost` refer to the
helper container. For MySQL on a macOS or Windows Docker host, accept the
prompt to use `host.docker.internal`; on ECS, normally enter the PolarDB
endpoint.

The generated `.env` has mode `0600`. Back it up because restarts and upgrades
must keep using its `PAS_ENCRYPTION_KEY`. See the
[Docker Compose deployment reference](../deployment/docker-compose.md) for
image mirrors, skipping the connection test, and automated generation.

If a GHCR download temporarily fails, retry:

```bash
docker pull "ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}"
```

You can also use the offline image archive from the
[v0.0.6 Release](https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/releases/tag/v0.0.6).

## Step 4: Migrate and start

Run the database migration before starting the service:

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml run --rm migrate
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml up -d server
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml ps
curl --fail http://127.0.0.1:18760/readyz
```

<p align="center">
  <img src="../../zh-cn/getting-started/images/deploy-migrate-start.png" alt="Migration, start, and readyz check output" width="820">
</p>

`--env-file .env` is required. Start `server` only after `migrate` exits
successfully.

## Step 5: Create the administrator

On first start, the bootstrap token is printed to the container logs:

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml logs server
```

<p align="center">
  <img src="../../zh-cn/getting-started/images/bootstrap-token-logs.png" alt="Bootstrap token in the container logs" width="820">
</p>

Open `http://<ECS public address>:18760/setup` and enter the token:

<p align="center">
  <img src="../../zh-cn/getting-started/images/setup-enter-token.png" alt="Enter the bootstrap token on the setup page" width="820">
</p>

Then create the first administrator with a password of at least 12 characters:

<p align="center">
  <img src="../../zh-cn/getting-started/images/setup-admin-password.png" alt="Create the first administrator" width="820">
</p>

If the token expired or must be reissued, see
[Initial setup](../setup/initial-setup.md).

For a multi-replica deployment, see the
[Kubernetes deployment guide](../deployment/kubernetes-helm.md) and
[ACK with PolarDB](../deployment/ack-polardb.md).

Next: [Feature usage 1: guided configuration](./configure.md).
