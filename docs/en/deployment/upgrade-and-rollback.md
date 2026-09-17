# Upgrade and rollback

[简体中文](../../zh-cn/deployment/upgrade-and-rollback.md)

The target schema must be validated before any target-version Pod serves
traffic. Compose and Helm run migration before replacing the application.
A managed rolling upgrade may start one target-version Pod while it remains
drained, run the deterministic one-shot migration Pod, validate schema and
health, and only then return that Pod to traffic.

## Before upgrading

1. Read the target release notes and known issues.
2. Back up the metadata database and verify that it can be restored.
3. Back up the exact `PAS_ENCRYPTION_KEY` separately.
4. Record the current image digest, Chart values, database revision, and
   configuration version.
5. Verify the new checksums, attestations, SBOM, and image digest.
6. Run `pas database check` with the candidate binary, the original metadata
   database, and its original root key before starting application replicas.

The check fails closed with `DATABASE_ENCRYPTION_KEY_MISMATCH` when the key
cannot decrypt persisted configuration. Do not generate a replacement key or
delete the database to make an upgrade proceed. Restore the matched recovery
set first.

## Schema compatibility and recovery

`pas database inspect --format json` reports the current revision, pending
revisions, release classification and compatibility without changing schema or
configuration. `pas database check --format json` additionally validates the
root key and stored configuration. Existing commands without these options
remain supported.

The generation-1 bridge was introduced at revision `fb1c2d3e4f5a`. Explicit
migration publishes a compatibility record in existing `system_config` rows;
startup reads this record and verifies required physical columns. Unknown
revisions without a supported contract remain rejected. A source release can
remain running after a later expansion only when it already understands the
published bridge contract and its source-to-target path has been verified.

Migration commands serialize access to one metadata database and retain
execution records. `DATABASE_MIGRATION_BUSY` means another executor holds the
lock. `DATABASE_MIGRATION_REPAIR_REQUIRED` means an interrupted operation left
an unrecognized physical state; preserve evidence and repair it before retrying.
Fresh initialization failures before `system_config` exists are diagnosed from
the migration executor logs. Do not use automatic downgrade to roll back an
expanded database.

`pas database migrate` writes structured progress records to standard error so
init-container and one-shot Pod logs show the active stage. Records include the
bundled and required heads, release classification, database connection state,
whether schema changes were attempted, the sanitized error code, and the next
operator action. They never include the database URL or credentials. A
manifest-validation failure explicitly confirms that no database connection
was opened and no schema change was attempted.

In managed mode, a target process waiting for an uninitialized or outdated
schema serves `/livez` while `/readyz` and business requests return 503. It
rechecks compatibility every two seconds for up to 30 minutes and starts
business services after the external migration succeeds. It never performs DDL
on startup. Other schema failures remain blocked for operator diagnosis.


The bundled manifest is authoritative; there is no user-supplied `--manifest`
override. `pas database migrate --operation-id <stable-id>` associates an external
operation with the same database/manifest checkpoint. The executor accepts only
`NONE` or additive `EXPAND` manifests whose head and sequence match the bundled
Alembic history. `CONTRACT` and `BREAKING` releases require a separate maintenance
procedure.

### v0.0.12 schema boundary

v0.0.12 advances the metadata revision from `fb1c2d3e4f5a` to
`9c1d2e3f4a5b`. The release class is `EXPAND`: it adds knowledge-scope tables,
columns with stable defaults, and non-unique indexes without removing or
tightening existing objects.

Public v0.0.11 predates the generation-1 bridge contract. Upgrading a public
v0.0.11 installation is therefore a maintenance upgrade, not a mixed-version
rolling upgrade:

1. Stop application replicas and metadata writers.
2. Back up the metadata database and matching `PAS_ENCRYPTION_KEY`.
3. Run `migrate -> check -> migrate -> check` with the v0.0.12 image.
4. Start only v0.0.12 replicas and complete an authenticated smoke test.

After the migration, do not roll only the application image back to public
v0.0.11; its exact-head check rejects revision `9c1d2e3f4a5b`. Recover with a
forward fix, or stop writers and restore the matched pre-upgrade database,
encryption key, image digest, and deployment values.

The authenticated management listener provides `GET /api/internal/v1/schema`
with `instance_id` and `generation`. A strict controller checks the immutable
manifest digest, schema/configuration compatibility, runtime `READY`, and
`business_smoke: PASSED`. The smoke performs an authenticated, read-only Agent API
request inside that replica; its temporary credential is never returned. The old
`/api/internal/v1/status` contract is unchanged. The controller must check every
replica even when the shared migration checkpoint has already succeeded.

[Knowledge activation](../knowledge/activation.md) is a separate configuration
rollout. Complete the feature-capable image rollout before changing this setting;
older images do not enforce the new knowledge admission gate.

## Compose

Set `PAS_IMAGE` to the new immutable digest, then run:

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose run --rm migrate database check
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
PAS_VERSION=X.Y.Z
helm upgrade pas "./polardb-agentic-server-${PAS_VERSION}-chart.tgz" \
  --namespace pas-system \
  --set existingSecret=pas-bootstrap \
  --set image.repository=REGISTRY/polardb-agentic-server \
  --set image.digest=sha256:DIGEST \
  --wait --timeout 10m
```

Verify the migration Job, rollout status, `/readyz`, configuration convergence,
and an authenticated smoke test.

## Managed rolling upgrade

Upgrade replicas serially:

1. Select one Pod and drain it from RS traffic.
2. Switch the Pod to the target image.
3. Run or replay the deterministic one-shot schema migration Pod for the
   logical PAS instance while the Pod remains drained.
4. Validate the physical schema and target-image health.
5. Return the Pod to RS traffic, then continue with the next replica.

The managed executor creates a CoreV1 Pod with `restartPolicy: Never` from the
target PAS image. It uses the same Secret and runtime configuration as the
application but has an independent lifecycle and exit status. The provider
requires only `create` and `get` for Pods for this step; it does not require
Batch Job RBAC or `pods/exec` access to an application Pod.

Schema health is the migration Pod exiting successfully after
`migrate -> check -> migrate -> check`. The second migration proves replay
idempotency, and each `pas database check` validates the Alembic revision,
recognized physical schema, root-key decryption, and stored configuration
compatibility without changing the database.

Service health is a separate gate. PAS runs the same read-only database
compatibility check before routes and workers start. Kubernetes then evaluates
`/livez` for process liveness and `/readyz` for metadata database and loaded
configuration readiness. The managed provider also requires the target image,
Running/Ready Pod and container state, completed ENI injection, and the
expected Pod IP before restoring RS traffic.

The first replica performs the mutation. Later replicas replay the same
completed migration identity and verify its result. The migration command and
workflow step must be idempotent so a transient workflow retry does not create
a second independent mutation.

If migration or validation fails, stop the workflow with the current Pod still
drained. Keep the failed migration Pod, logs, revision, and physical-schema
evidence for manual intervention. Do not restore its traffic or advance
remaining replicas. Old replicas may continue serving only because managed
rolling upgrades permit `NONE` or verified `EXPAND` migrations; destructive
`CONTRACT` or `BREAKING` DDL requires a separate maintenance procedure.

An operator can inspect and then explicitly retry a failed migration:

```bash
kubectl -n <namespace> logs <migration-pod> -c db-migrate
kubectl -n <namespace> delete pod <migration-pod>
```

Delete the Pod only after preserving the failure evidence and correcting the
underlying problem. Replaying the same workflow step recreates the same
deterministic migration identity.

## Known same-revision schema repair

Some managed pre-release databases can report Alembic revision
`f6a7b8c9d0e1` while missing schema objects that were added to that revision's
ancestry. The release migration command recognizes only the complete legacy
fingerprint, applies the skipped migrations, and keeps the revision at
`f6a7b8c9d0e1` so the recorded rollback boundary does not change.

Retain the migration executor logs. `LEGACY_F6_SCHEMA_UNKNOWN` means the database
does not match the supported legacy fingerprint.
`LEGACY_F6_SCHEMA_PARTIAL` means some repair artifacts already exist but the
step is incomplete. Both conditions fail closed and require schema inspection;
do not stamp the database or create individual tables or columns manually.

Application startup and `pas database check` report
`DATABASE_SCHEMA_REPAIR_REQUIRED` before a supported repair,
`DATABASE_SCHEMA_REPAIR_PARTIAL` for a partially applied repair, and
`DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN` when the physical schema does not
match either supported state. These checks are read-only. Run the migration
with the target image, then run it a second time to verify idempotency before
the drained target Pod returns to traffic.

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

## Global audit configuration version 2 rollout

The SQL Security and Observability documents form one global audit
configuration pair. New binaries project the previous version 1 pair into the
version 2 runtime model without rewriting the database during startup,
`pas database check`, configuration reads, or runtime polling. Old replicas can
therefore continue reading version 1 while the application rollout is in
progress.

Pause SQL Security and Observability configuration writes before a rolling
deployment. Deploy the new binary to every PAS replica and verify `/readyz` and
configuration convergence on every replica before resuming writes. Before
processing the first state-changing request to either module, PAS atomically
converts both documents to version 2. Requests with a stale revision are
rejected before conversion. Do not run old replicas after that conversion
because they cannot read version 2.

Before the first version 2 write, an image-only rollback is supported. After
that write, keep the new binary and deploy a forward fix, or restore the
matched pre-upgrade `system_config` backup and `PAS_ENCRYPTION_KEY` before
starting the old binary. A Helm rollback does not restore these documents.

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
