# Backup and restore

[简体中文](../../zh-cn/operations/backup-and-restore.md)

A recoverable PAS deployment requires both the metadata database and the
matching root encryption key.

## Backup

Use a database-native consistent backup before migrations and on the
organization's recovery schedule. Back up `PAS_ENCRYPTION_KEY` independently
in an approved secret manager. Record the application image digest and
Alembic revision with the backup.

Application logs are optional for functional restore but may be required for
audit policy. Docker named volumes and Kubernetes `emptyDir` logs are not a
substitute for external backup.

## Restore

Provision a compatible MySQL or PostgreSQL database, restore metadata, and
provide the exact original root key. Start the same application version and
run `pas database check` before serving traffic. If upgrading after restore,
back up the restored database and run migration first.

## Recovery limits

Losing or changing the root key makes encrypted module secrets, database
credentials, Agent Tokens, and shared JWT key material unreadable. There is
no automatic key reconstruction. A database backup without its key is not a
complete recovery set.

Restoring an older database may also restore older Token and binding state.
Revoke or rotate affected credentials after a disaster recovery exercise.

## Verification

Regularly test restoration in an isolated environment. Verify schema state,
administrator login, configuration readiness on every replica, MCP
authentication, registered-instance tests, and one controlled SQL query.
