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

## Helm

The Chart's `pre-upgrade` migration Job blocks the Deployment update:

```bash
helm upgrade pas ./polardb-agentic-server-0.0.2-chart.tgz \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

Verify the migration Job, rollout status, `/readyz`, configuration convergence,
and an authenticated smoke test.

## Rollback limits

Alembic migrations are forward operations; the supported release process does
not automatically downgrade the metadata schema. Rolling only the image back
is safe only when the older release explicitly supports the migrated schema.
Otherwise stop writers, restore the pre-upgrade database backup, restore the
same root encryption key, and redeploy the recorded prior image digest and
values. A Helm revision rollback does not restore the database.
