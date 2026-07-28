# Credential and key rotation

[简体中文](../../zh-cn/operations/credential-and-key-rotation.md)

Rotate credentials according to their owner and lifecycle. Database
credentials, Agent Tokens, cloud AccessKeys, OIDC secrets, and the PAS root key
have different procedures.

## Database and Agent credentials

After a MySQL password changes, edit the existing instance credential, enter
the new password, and run **Test Connection** before saving. Do not recreate
the physical instance. Review bindings that reference revoked credentials.

Regenerating an Agent Token invalidates the old value immediately. Update the
client secret and reconnect. Use revocation when a client is retired.

## Cloud and SSO secrets

Edit `aliyun_access` or `user_sso`, run dry run from the backend, validate,
and activate. In VPC mode verify both STS and PolarDB endpoints. Keep the old
credential valid only for the controlled overlap required by the external
provider, not as a PAS dual-secret mechanism.

## Root encryption key

Version `0.0.2` does not provide online root-key re-encryption. Do not replace
`PAS_ENCRYPTION_KEY` on a running database. A changed key causes fail-closed
decryption errors. Preserve and restore the original key with the database.

## Validation

After rotation, verify readiness on all replicas, administrator or Agent
authentication as appropriate, a backend connection test, and Audit Logs.
Remove temporary secret files and old values from the external secret manager
only after verification.
