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

## Aliyun Access mode changes

When switching `aliyun_access` modes, PAS stops using the prior mode only after
the administrator saves the change. **Clear the previous credential** is the
recommended default and removes the old mode block. **Retain it, but keep it
disabled** keeps that block encrypted but inactive; it is not loaded, validated,
or selected as a fallback.

To return to a retained mode, choose **Use retained credential** explicitly,
then run dry run and save with the normal confirmation path. Alternatively,
replace the credential with a new value. Reusing a retained direct AccessKey as
the source for AssumeRole is a distinct confirmed action, never an automatic
copy. Choose **Delete retained credential** to remove an inactive retained
block when recovery is no longer required.

The console shows an AccessKey ID only through a short server-generated display
mask. It never returns an AccessKey Secret, External ID, STS temporary
credential, or security token. Treat a new secret entry as a rotation and
enter it only through the configured secure input or CLI secret reference.

## Root encryption key

The current release does not provide online root-key re-encryption. Do not
replace `PAS_ENCRYPTION_KEY` on a running database. A changed key causes
fail-closed decryption errors. Preserve and restore the original key with the
database.

## Validation

After rotation, verify readiness on all replicas, administrator or Agent
authentication as appropriate, a backend connection test, and Audit Logs.
Remove temporary secret files and old values from the external secret manager
only after verification.
