# CLI reference

**English** | [简体中文](../../zh-cn/reference/cli.md)

The `pas` command operates the metadata database, starts the server, and
performs guided configuration. Supply `PAS_DATABASE_URL` and
`PAS_ENCRYPTION_KEY` before commands that access local service state.

## Database lifecycle

```bash
pas database check
pas database migrate
```

`check` is read-only. It succeeds only when the database revision equals the
single Alembic head bundled with PAS. `migrate` runs `alembic upgrade head`.
Run it once before starting or upgrading application replicas and back up
production metadata first.

Database failures use stable codes:

- `DATABASE_SCHEMA_NOT_INITIALIZED`
- `DATABASE_SCHEMA_OUTDATED`
- `DATABASE_SCHEMA_TOO_NEW`
- `DATABASE_MIGRATION_HEAD_INVALID`
- `DATABASE_UNAVAILABLE`

Messages never include the password or full database URL. PAS does not
automatically downgrade or migrate during `serve`.

## Start the service

```bash
pas serve
```

Startup performs the same read-only schema check before configuration, JWT
keys, or background workers initialize. Use `pas database migrate` explicitly
when the schema is empty or behind.

## Guided configuration

```bash
pas config modules
pas config show <module>
pas config configure [module]
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
pas config export --file current.yaml
```

See [guided modular configuration](../configuration/guided-configuration.md)
for bootstrap-token sources, secret references, module states, validation, and
activation.

## Bootstrap token recovery

```bash
pas config bootstrap-token issue \
  --output /var/run/pas/bootstrap-token
```

Run this command in one selected Pod. The target must be an absolute new file
on a restricted writable volume. Copy it through a secret-safe channel, use it
for setup, and remove both copies after consumption.
