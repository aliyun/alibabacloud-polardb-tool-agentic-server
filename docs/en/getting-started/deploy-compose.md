# Deployment (single ECS + Docker Compose)

**English** | [简体中文](../../zh-cn/getting-started/deploy-compose.md)

This page deploys PAS on a single ECS with Docker Compose, pointing the
metadata database at the PolarDB MySQL prepared in
[Resource requirements](./cloud-resources.md).

## Prerequisites

- SSH access to the ECS with sudo privileges.
- The PolarDB endpoint, database name, username, and password.
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

Set `PAS_VERSION` to the release you want, then download that tag's source
archive from GitHub. It contains the Compose deployment files under
`deploy/compose/`, and Git is not required:

```bash
PAS_VERSION=0.0.5
wget "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/archive/refs/tags/v${PAS_VERSION}.tar.gz"
tar -xzf "v${PAS_VERSION}.tar.gz"
cd "alibabacloud-polardb-tool-agentic-server-${PAS_VERSION}"
```

Run all later commands inside this directory. The service image does not need
a manual download; Compose pulls the image selected by that release's
deployment files automatically on start, and you may pre-pull it with
`docker pull`. If you prefer Git, you can `git clone` the repository and
check out the matching `v${PAS_VERSION}` tag instead; the directory layout is
the same.

## Step 3: Prepare .env

Generate `PAS_DATABASE_URL` and `PAS_ENCRYPTION_KEY` with the release's
containerized helper:

```bash
scripts/deploy/create-external-mysql-env.sh
```

The host runs only a POSIX shell and Docker; Python, SQLAlchemy, and the
`asyncmy` driver run inside the selected PAS image. The helper prompts for
the endpoint, port (default `3306`), database name, username, and password.
It displays the non-secret fields and asks `Use these settings? [Y/n]`;
answer `n` to re-enter them. Password input is displayed as `*` characters
without revealing its value. The helper safely encodes special characters,
constructs a `mysql+asyncmy` URL, and prints the endpoint, database, username,
and `SELECT 1` action before generating the encryption key or creating
`.env`. A failure reports a sanitized reason such as authentication failure,
unknown database, name-resolution failure, or an unreachable endpoint.

When the helper runs through Docker, `127.0.0.1` and `localhost` refer to the
generator container, not the host. For MySQL running on macOS or Windows with
Docker Desktop, the helper asks `Use host.docker.internal instead? [Y/n]`
and actually replaces the endpoint when you accept. Otherwise use a DNS name
or IP address reachable from the container.

On success, `.env` is mode `0600`. The helper refuses to overwrite an
existing path. A connection or write failure leaves no new environment file.
Do not pass database fields or passwords as command-line arguments.

If the release image is mirrored into another registry, select it explicitly:

```bash
scripts/deploy/create-external-mysql-env.sh \
  --image registry.example/pas:VERSION
```

An explicit `--image` is also saved as `PAS_IMAGE` so later Compose commands
use the same image. Inherited `PAS_IMAGE`, `PAS_DATABASE_URL`, and
`PAS_ENCRYPTION_KEY` values are ignored by the helper. On mainland China
networks, mirror the image into an accessible registry before running this
command.

The connection test fails closed by default. Only when the database cannot
be reached yet and you intentionally accept an unverified configuration, use:

```bash
scripts/deploy/create-external-mysql-env.sh --skip-connection-test
```

This option prints a warning. Migration or startup can still fail if the
connection details are wrong.

For advanced automation that cannot use an interactive terminal, construct
the file manually only after percent-encoding each URI component. Keep the
single quotes so Compose treats characters such as `$` literally:

```dotenv
PAS_DATABASE_URL='mysql+asyncmy://USER:PERCENT_ENCODED_PASSWORD@ENDPOINT:3306/DATABASE'
PAS_ENCRYPTION_KEY='BASE64_ENCODED_32_BYTE_KEY'
```

Never place a raw password containing URI delimiters into the URL. Do not
copy `.env.compose.example`; it contains `MYSQL_ROOT_PASSWORD` and similar
variables and is only for the root `compose.yaml` with bundled MySQL. You may
append `PAS_PORT` if needed. Back up `.env`; restarts and upgrades must use
the same root key.

## Step 4: Migrate and start

The v0.0.3 and later images install the `pas` entry point and server package
without requiring a `PYTHONPATH` override. If you are upgrading a v0.0.1
deployment that added `PYTHONPATH: /app` as a workaround, remove that line
from `deploy/compose/compose.external-mysql.yaml`.

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
