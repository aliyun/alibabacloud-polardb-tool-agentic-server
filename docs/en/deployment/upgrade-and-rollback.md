# Upgrade and rollback

[简体中文](../../zh-cn/deployment/upgrade-and-rollback.md)

Treat an upgrade as a database migration followed by an application rollout.
Never start new application Pods before the migration succeeds.

## Before upgrading

1. Read the target release notes and known issues.
2. Back up the metadata database and verify that it can be restored.
3. Back up the exact `PAS_ENCRYPTION_KEY` separately.
4. Record the current image digest, Chart values, database revision, and
   configuration version.
5. Verify the new checksums, attestations, SBOM, and image digest.

## Compose

Set `PAS_IMAGE` to the new immutable digest, then run:

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose up -d --no-deps server
curl --fail http://127.0.0.1:18760/readyz
```

Do not update the server if migration fails.

When upgrading from a v0.0.1 Compose deployment, remove the historical
`PYTHONPATH: /app` workaround from the Compose environment. The v0.0.3 image
installs the application and `pas` entry point correctly without that
override.

## Helm

The Chart's `pre-upgrade` migration Job blocks the Deployment update:

```bash
PAS_VERSION=0.0.7
helm upgrade pas "./polardb-agentic-server-${PAS_VERSION}-chart.tgz" \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

Verify the migration Job, rollout status, `/readyz`, configuration convergence,
and an authenticated smoke test.

## Aliyun Access version 2 rollout

The `aliyun_access` credential document is version 2. New binaries read both
the previous direct configuration and version 2, but an old binary cannot
safely read version 2. The first Aliyun Access save converts the complete
module document atomically; startup alone does not rewrite it.

The write pause is operator-enforced, not a server maintenance-mode feature.
Before a rolling deployment, restrict administrator access or otherwise pause
all Aliyun Access configuration writes. Deploy the new binary to every PAS
replica, then verify each replica has loaded the desired configuration version
with `/readyz` before allowing the first save or mode switch. Do not run mixed
old and new replicas after a version 2 write.

Before the upgrade, back up `system_config` in the metadata database and the
exact `PAS_ENCRYPTION_KEY` (the configuration master key) as a pair. A database
backup without its matching key cannot decrypt recovered credentials. Retain
the recorded direct configuration and image digest as a recovery option.

## Auto-provisioning pool rollout

Apply the additive schema with `dedicated_pool_enabled=false`. Verify existing
multitenant create/describe/delete behavior first. Then deploy the new binary
to every replica, activate bounded purchase/member/Agent budgets, enable the
flag, restart replicas, and validate one internal `destroy` pool before
allowing pilot Agent bindings. Enable administrator restore next; enable
`sanitize_and_reuse` last.

PAS checks the Alembic head before configuration, routes, or workers activate.
An older binary rejects a newer unknown revision with
`DATABASE_SCHEMA_TOO_NEW`. The new binary must be present on every replica
before Dedicated records are created; mixed old/new processing is unsafe.

## Rollback limits

Alembic migrations are forward operations; the supported release process does
not automatically downgrade the metadata schema. Rolling only the image back
is safe only when the older release explicitly supports the migrated schema.
Otherwise stop writers, restore the pre-upgrade database backup, restore the
same root encryption key, and redeploy the recorded prior image digest and
values. A Helm revision rollback does not restore the database.

Before the first Dedicated resource is created, disable the flag and roll back
only when the previous binary explicitly supports the migrated schema. After a
Dedicated resource or member exists, disable entry points and workers but keep
the new binary/data model, then deploy a forward fix. Do not run an old binary
against Dedicated states: it may double-allocate, disclose credentials, or
delete the wrong physical cluster. A database rollback requires the matched
pre-upgrade metadata backup and `PAS_ENCRYPTION_KEY`.

For Aliyun Access specifically, rollback before the first version 2 write is
direct. After the first version 2 write, including a draft save that has not
been activated, the old binary cannot safely run against the database. Use the
new binary to switch back to a compatible `direct_ak` direct mode before
starting the old binary, or restore both the pre-upgrade database backup and
its matching configuration master key. An AssumeRole or ECS RAM role
configuration must be changed to direct mode or restored from that matched
backup before the older binary runs.
