# Compatibility and version policy

[简体中文](../../zh-cn/reference/compatibility.md)

Version `0.0.1` is the first public trial and is published as a pre-release.
The `0.0.x` line may correct contracts while user evaluation continues.
Version `0.1.0` is reserved for the first stabilized feature line after trial
feedback and critical defect resolution.

Version `0.0.2` is a patch release. It persists stale resource-pool
placeholder cleanup when the configured target is already satisfied and
returns `DATABASE_REQUIRED` when MySQL reports that no database is selected.
Agent Token-only deployments also restart safely with an HTTP VPC
`external_base_url`. It does not require a metadata schema migration from
`0.0.1`.

## Runtime compatibility

- Python runtime: 3.11 or later for source installation.
- Container platforms: Linux `amd64` and `arm64`.
- Metadata databases: MySQL 8.0 and PostgreSQL using the shipped async drivers.
- Recommended ACK metadata service: PolarDB for MySQL 8.0.
- Kubernetes: 1.27 or later; Helm: 3.12 or later.

SQLite is supported for local development and tests, not production
multi-replica deployment.

## Upgrade compatibility

Database compatibility is determined by the Alembic revision, not the
application version string. Run the release's migration Job or
`pas database migrate` once before rolling out its application Pods. Startup
fails closed for empty, older, newer, unavailable, or ambiguous schema state.

Downgrading application code after a forward migration is not generally safe.
Restore a compatible backup instead of attempting an automatic downgrade.

## API and configuration

The guided configuration API currently uses `protocol_version: 1`; stored
module documents carry their own schema version. Clients must not assume a
future protocol can read an unknown module schema. MCP tool visibility is
authorization-dependent and may change after reconnecting.

## Release artifacts

Use immutable Git tags, image digests, checksums, and attestations. A released
tag or asset is never replaced; fixes receive a new patch version. Documentation
links for a deployed version should point to that release tag.
