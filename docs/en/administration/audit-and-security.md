# Audit and security

[简体中文](../../zh-cn/administration/audit-and-security.md)

PAS records security-relevant administration, authentication, SQL, binding,
credential, and provisioning events. Audit records support investigation but
do not replace database-native audit or cloud control-plane logs.

## Audit scope

Review actor, action, target, result, timestamp, request context, and sanitized
failure category. SQL policy records blocked or confirmed operations without
making credentials public. Configuration audit records changed field names,
revision, and state while excluding secret values.

The console separates the same audit stream into **All**, **SQL**, and
**PolarRAG** views. PolarRAG Tool records include resolved instance, Space, KB,
hit-count, and partial-failure metadata. PolarRAG remains the document ACL
authority; PAS does not record the trusted ACL context or the search text.

## Secret boundary

Database passwords, Agent Tokens, bootstrap tokens, root keys, AccessKeys,
OIDC secrets, ciphertext, and SQL parameters must not appear in ordinary logs
or public error messages. Credential reveal and rotation are explicit audited
administrator workflows.

## Runtime dependency baseline

Deploy from the repository's `uv.lock`. The runtime requires PyJWT 2.15.0 or
later, AnyIO 4.14.2 or later, urllib3 2.8.0 or later, certifi 2026.10.19 or
later, aiohttp 3.14.5 or later, and multidict 6.9.1 or later. Preserve these
minimums when installing PAS with custom dependency constraints. aiohttp also
requires yarl 1.25.1 or later within its supported version range.

Malformed JWT headers and payloads are rejected as authentication failures.
Keep TLS certificate and hostname verification enabled for identity-provider,
cloud SDK, and other outbound HTTPS connections.

The certifi bundle excludes the compromised `emSign ECC Root CA - G3` trust
root while retaining unaffected roots. Rebuild custom images from the updated
lockfile and restart existing processes to load the new trust store; changing
the lockfile alone does not update a running container. If you supply a custom
CA bundle or system trust store, review it separately and remove the affected
root. Keep certificate verification enabled. aiohttp preserves Secure and
host-only cookie boundaries and handles oversized cookie expiry values without
breaking client connection reuse; multidict releases temporary references
after items-view set operations.

## Destructive operations

PAS applies capability checks and confirmation requirements, but the MySQL
account remains the final database permission boundary. A confirmation flag
does not grant a missing privilege. `DROP DATABASE` may remain blocked even
when other confirmable statements are allowed.

## Retention and export

Configure audit and application-log retention according to organizational
policy and available storage. Export records through approved channels before
expiration. Restrict log, backup, and support-bundle access because metadata
can still be operationally sensitive even after secrets are removed.
