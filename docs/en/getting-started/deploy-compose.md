# Deployment (single ECS + Docker Compose)

**English** | [简体中文](../../zh-cn/getting-started/deploy-compose.md)

This page deploys PAS on a single ECS with Docker Compose, pointing the
metadata database at the PolarDB MySQL prepared in
[Resource requirements](./cloud-resources.md).

## Prerequisites

- SSH access to the ECS with sudo privileges.
- The PolarDB `PAS_DATABASE_URL` connection string.
- The ECS security group already allows TCP 18760.

## Step 1: Install Docker

After logging in to the ECS, install Docker Engine and the Compose plugin:

```bash
curl -fsSL https://get.docker.com | sh
sudo docker version
sudo docker compose version
```

Note: Compose is a separate plugin, and some installation methods (such as
installing docker directly with yum) do not include it. If
`docker compose version` reports that compose is not a docker command,
install the plugin separately:

```bash
sudo yum -y install docker-compose-plugin
sudo docker compose version
```

For a detailed installation guide, see
[Install and use Docker on ECS](https://help.aliyun.com/zh/ecs/user-guide/install-and-use-docker).

## Step 2: Download the deployment files

Download the `v0.0.1` tag source archive from GitHub (it contains the Compose
deployment files under `deploy/compose/`); Git is not required:

```bash
wget https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/archive/refs/tags/v0.0.1.tar.gz
tar -xzf v0.0.1.tar.gz
cd alibabacloud-polardb-tool-agentic-server-0.0.1
```

Run all later commands inside this directory. The service image does not need
a manual download; Compose pulls
`ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:0.0.1` automatically
on start, and you may pre-pull it with `docker pull`. If you prefer Git, you
can `git clone` the repository and check out the `v0.0.1` tag instead; the
directory layout is the same.

## Step 3: Prepare .env

The external metadata database path needs `PAS_DATABASE_URL` and
`PAS_ENCRYPTION_KEY` provided directly. Create `.env` and generate the root
key:

```bash
cat > .env <<'EOF'
PAS_DATABASE_URL=mysql+asyncmy://USER:PASSWORD@ENDPOINT:3306/DATABASE
EOF
chmod 0600 .env
python3 -c 'import base64,os; print("PAS_ENCRYPTION_KEY="+base64.b64encode(os.urandom(32)).decode())' >> .env
```

Edit `.env`:

- Replace `PAS_DATABASE_URL` with the PolarDB connection string produced in
  [Resource requirements](./cloud-resources.md).
- Append `PAS_IMAGE` and `PAS_PORT` as needed; on mainland China networks,
  mirror the image into an accessible registry first and reference the
  mirror.
- Note: the repository's `.env.compose.example` (with `MYSQL_ROOT_PASSWORD`
  and similar variables) targets the bundled-MySQL `compose.yaml` and is not
  used by this tutorial's external metadata database path.
- Back up `.env`; restarts and upgrades must use the same root key.

<p align="center">
  <img src="../../zh-cn/getting-started/images/env-file-example.png" alt="Example of a filled .env" width="820">
</p>

## Step 4: Migrate and start

The v0.0.1 image has a known issue: when started as-is, the web console
(including the `/setup` page) returns `Not Found`. Before starting, edit
`deploy/compose/compose.external-mysql.yaml` and add `PYTHONPATH: /app` to
the `environment:` block:

```yaml
  environment: &pas-environment
    PAS_DATABASE_URL: ${PAS_DATABASE_URL:?set PAS_DATABASE_URL}
    PAS_ENCRYPTION_KEY: ${PAS_ENCRYPTION_KEY:?set PAS_ENCRYPTION_KEY}
    PYTHONPATH: /app
```

For an already-running service, add the line and run `up -d server` again to
apply it.

Use the Compose file for an external metadata database, migrating before
starting:

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml run --rm migrate
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml up -d server
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml ps
curl --fail http://127.0.0.1:18760/readyz
```

<p align="center">
  <img src="../../zh-cn/getting-started/images/deploy-migrate-start.png" alt="Migration, start, and readyz check output" width="820">
</p>

`--env-file .env` is required: when `-f` points at a Compose file in a
subdirectory, Compose does not load `.env` from the current directory
automatically. `server` starts only after `migrate` exits successfully. If
the migration did not reach the expected Alembic head, the server refuses to
start.

## Step 5: Claim ownership

On first start, the bootstrap token is printed to the container logs:

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml logs server
```

<p align="center">
  <img src="../../zh-cn/getting-started/images/bootstrap-token-logs.png" alt="Bootstrap token in the container logs" width="820">
</p>

If the logs are unavailable or the token has expired, reissue and view it
inside the container. The issue command refuses to write to an existing
file, so remove the old file before reissuing:

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  rm -f /var/run/pas/bootstrap-token
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  pas config bootstrap-token issue --output /var/run/pas/bootstrap-token
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  cat /var/run/pas/bootstrap-token
```

Reissuing invalidates the old token immediately; the new token is valid for
15 minutes. Delete the token file after setup completes.

Open `http://<ECS public address>:18760/setup` in a browser, enter the token,
and create the first administrator (password at least 12 characters). Note:
the console page is returned only to browser requests (`Accept: text/html`);
hitting the root path with `curl` returns `Not Found`, which does not mean
the service is broken. The web console and the API are both served on port
18760. The full token lifecycle and recovery procedures are in
[Initial setup](../setup/initial-setup.md).

<p align="center">
  <img src="../../zh-cn/getting-started/images/setup-enter-token.png" alt="Enter the bootstrap token on the setup page" width="820">
</p>

Set the administrator password:

<p align="center">
  <img src="../../zh-cn/getting-started/images/setup-admin-password.png" alt="Create the first administrator" width="820">
</p>

## Kubernetes / ACK multi-replica (note)

This tutorial currently features the single ECS + Docker Compose path. For a
multi-replica production deployment, the Helm Chart is published as the OCI
artifact `oci://ghcr.io/aliyun/charts/polardb-agentic-server`; see the
[Kubernetes deployment guide](../deployment/kubernetes-helm.md) and
[ACK with PolarDB](../deployment/ack-polardb.md).

Next: [Feature usage 1: guided configuration](./configure.md).
