# Initial setup

**English** | [简体中文](../../zh-cn/setup/initial-setup.md)

This guide covers the first start of the service, bootstrap-token delivery,
creation of the first administrator, and recovery when the original token is
unavailable. Continue with guided modular configuration after ownership has
been established.

## Bootstrap settings

The process accepts exactly two startup settings:

- `PAS_DATABASE_URL`: the metadata database connection URL. Use durable MySQL
  or PostgreSQL storage for production.
- `PAS_ENCRYPTION_KEY`: the root encryption key, supplied through a secret
  environment variable or `file:/absolute/restricted/path`.

`PAS_ENCRYPTION_KEY` must be Base64 that decodes to exactly 32 bytes. Every
replica must use the same metadata database and root key. Back up the database
and root key separately; losing the root key makes encrypted configuration,
credentials, and shared JWT signing keys unrecoverable.

Database pooling and the listening address are process constants. They are not
additional setup settings.

## Prepare and start the service

For a local SQLite test:

```bash
uv sync --extra dev

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="$(
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
)"

uv run pas database migrate
uv run pas database check
uv run pas serve
```

Use a persistent MySQL or PostgreSQL metadata database in Docker and
Kubernetes. Run `pas database migrate` as a deployment migration step before
starting or rolling out application replicas. `pas database check` is
read-only and reports whether the database is at the single Alembic head
required by this application.

The application version does not determine database compatibility. Alembic
revision state does. `pas serve` performs the same read-only check and refuses
to initialize the service when the schema is empty, behind, newer than the
application, contains multiple heads, or cannot be reached. It never applies
DDL automatically. Back up production metadata before migration.

## Bootstrap token lifecycle

When the metadata database is empty, all replicas may attempt initialization,
but an atomic database insert selects exactly one winner. The winner creates
one bootstrap claim and prints the plaintext token once. Other replicas load
the shared initialized state and do not create competing tokens.

The token:

- is valid for 15 minutes;
- is stored in the database only as a SHA-256 hash;
- is rejected after 10 failed attempts;
- is invalidated when a replacement is issued;
- is consumed atomically when `core_admin` is activated.

Restarting the process against the same metadata database does not print the
token again. The server cannot recover or display the current plaintext token.

## Local setup

Open the setup UI, enter the token printed by the backend, and create the first
administrator. The administrator password must contain at least 12 characters.

The UI runs a read-only dry run first. A separate **Activate module** action
saves, validates, and activates the checked configuration. When the backend
reports `READY`, use **Enter administration console** to open `/dashboard`.
Runtime services are already running behind the setup access policy, so
standalone and multi-replica deployments do not require a restart after this
transition.

Terminal-only environments can run:

```bash
pas config init
```

The interactive command reads the bootstrap token without echo. Automation
must use exactly one of `PAS_BOOTSTRAP_TOKEN`,
`--bootstrap-token-stdin`, or
`--bootstrap-token-file /absolute/restricted/path`. Never place a token in a
URL, YAML declaration, or ordinary command-line argument.

## Docker

On the first start, the automatic token is part of the container's stdout and
can be read with:

```bash
docker logs <container-name>
```

Restrict access to container logs because they contain a password-equivalent
secret during initial setup. If the log is unavailable or the token has
expired, mount a restricted writable directory and issue a replacement:

```bash
docker exec <container-name> \
  pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token

docker exec <container-name> \
  cat /var/run/pas/bootstrap-token
```

Enter the displayed value in the setup UI, or run `pas config init` in the
same container with `--bootstrap-token-file`. Remove the file after ownership
has been established.

## Kubernetes with multiple replicas

All Pods must share `PAS_DATABASE_URL` and receive the same
`PAS_ENCRYPTION_KEY` from a Kubernetes Secret. Only the Pod that wins database
initialization prints the automatic token. Read all Pod logs with source
prefixes to find it:

All application Pods must also have equivalent DNS, routing, security-group,
and egress access to every registered MySQL endpoint. Instance and credential
connection tests are opened by the PAS Pod that handles the API request. This
matches the SQL over HTTP path but proves only that replica's connectivity.

```bash
kubectl logs -n <namespace> deployment/pas \
  --all-pods=true \
  --prefix \
  --since=30m
```

If the token is unavailable or expired, choose one exact Pod and use that Pod
for every file-based command:

```bash
kubectl get pods -n <namespace> \
  -l app.kubernetes.io/name=pas

POD=<pod-name>

kubectl exec -n <namespace> "$POD" -c pas -- \
  pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token

kubectl exec -n <namespace> "$POD" -c pas -- \
  cat /var/run/pas/bootstrap-token
```

The replacement claim is stored in the shared metadata database, so the setup
request may reach any healthy replica. The token file is Pod-local; do not use
two separate `kubectl exec deployment/pas` commands because a rollout or Pod
replacement can select a different Pod. Mount `/var/run/pas` as a restricted
`emptyDir` or equivalent ephemeral volume.

For a terminal-only setup, keep using the selected Pod:

```bash
kubectl exec -n <namespace> -it "$POD" -c pas -- \
  pas config \
  --bootstrap-token-file /var/run/pas/bootstrap-token \
  init
```

After the UI or CLI consumes the claim, remove the Pod-local file:

```bash
kubectl exec -n <namespace> "$POD" -c pas -- \
  rm /var/run/pas/bootstrap-token
```

The supported Helm Chart prints the same file-copy procedure in its NOTES.
See the [Kubernetes deployment guide](../deployment/kubernetes-helm.md) for
migration hooks, multi-replica readiness, and upgrades.

## Recovery

Use `pas config bootstrap-token issue --output <absolute-path>` when the
automatic token was not captured, expired, reached its failed-attempt limit,
or disappeared with a replaced Pod. Issuing a replacement atomically
invalidates the previous claim and writes a new file with mode `0600`.

The output path must be absolute and must not already exist. The command
refuses symlinks and does not print the token to stdout. If setup is already
complete, authenticate with the administrator account instead; bootstrap
tokens are not a continuing administration credential.

## Security checklist

- Restrict Docker log, Kubernetes log, `exec`, and secret access with
  least-privilege permissions.
- Do not send bootstrap tokens to centralized logs, shell history, manifests,
  support tickets, or source control.
- Use an ephemeral `0600` file only for the short setup window and delete it
  after consumption.
- Do not store `PAS_ENCRYPTION_KEY` in the same backup as the metadata
  database.
- Rotate an exposed, unconsumed token immediately by issuing a replacement.

After ownership is established, continue with
[guided modular configuration](../configuration/guided-configuration.md).
