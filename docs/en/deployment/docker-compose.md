# Docker Compose deployment

[简体中文](../../zh-cn/deployment/docker-compose.md)

The supported Compose stack runs three services in order: pinned MySQL 8.0,
one-shot metadata migration, and the PAS server. Only PAS port `18760` is
published to the host; MySQL remains on the private Compose network.

## Prepare secrets

Copy the example without committing the result:

```bash
cp .env.compose.example .env
chmod 0600 .env
```

Generate the root key directly into the restricted file rather than printing
it to CI logs:

```bash
python3 -c \
  'import base64,os; print(base64.b64encode(os.urandom(32)).decode())' \
  | sed 's/^/PAS_ENCRYPTION_KEY=/' >> .env
```

Remove the placeholder `PAS_ENCRYPTION_KEY` line, set strong and distinct
MySQL passwords, and protect a backup of `.env`. The same root key must be
used after every restart and upgrade.

`MYSQL_IMAGE` defaults to the tested digest. In a restricted or mainland
China network, mirror that exact image into an accessible registry and set
`MYSQL_IMAGE` to the mirrored reference before starting the stack.

## Start and inspect

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:18760/readyz
```

The `server` starts only after `migrate` exits successfully. Inspect an
upgrade failure with:

```bash
docker compose logs migrate
docker compose logs server
```

Use the bootstrap-token procedure in the initial setup guide to claim the
installation. Application logs and the Pod-local token exchange directory use
named volumes; MySQL data uses `mysql-data`.

## Backup and upgrade

Back up both the MySQL database and the encryption key before an upgrade.
Resolve the new release tag to a digest, set `PAS_IMAGE` to that exact image,
then run:

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose up -d --no-deps server
```

The server refuses to start if the migration did not reach the expected
Alembic head.

## External metadata database

For an existing MySQL 8.0 database:

```bash
export PAS_DATABASE_URL='mysql+asyncmy://USER:PASSWORD@HOST:3306/DATABASE'
docker compose \
  -f deploy/compose/compose.external-mysql.yaml \
  up -d
```

For PostgreSQL:

```bash
export PAS_DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE'
docker compose \
  -f deploy/compose/compose.external-postgres.yaml \
  up -d
```

Store `PAS_DATABASE_URL` and `PAS_ENCRYPTION_KEY` in a restricted env file or
secret manager rather than shell history.

## Stop or remove

`docker compose down` keeps named volumes. Only use
`docker compose down --volumes` after a verified backup when the metadata,
logs, and bootstrap exchange volume may be permanently deleted.
