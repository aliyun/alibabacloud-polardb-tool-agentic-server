# Enable and disable knowledge

**English** | [简体中文](../../zh-cn/knowledge/activation.md)

New installations start with knowledge disabled. MySQL registration, SQL access,
Agents, and authentication work independently. Upgrading an existing installation
without a `knowledge` configuration preserves its enabled behavior. This change
uses the existing metadata schema; enabling a feature never runs Alembic.

## Enable on a self-hosted instance

1. Sign in as an administrator and open **Features → Knowledge**.
2. Choose **Enable knowledge**. Select a saved connection or save a connection to
   an existing PolarRAG service. A saved connection is a draft, not proof of access.
   PAS does not create or purchase that service.
3. Check the connection, select a knowledge space and resource, and select an
   existing PAS user. **Verify read access** checks the selected identity through
   the protected search API. It does not grant permissions. Configure enterprise
   identity bindings first when the space requires enterprise ACLs.
4. Choose **Save and enable**. The state becomes `PENDING_RESTART`. Restart the PAS
   container or process, for example `docker compose restart pas` when the Compose
   service is named `pas`. Use the service name in your deployment file.
5. Wait for `ENABLED`, then reconnect MCP clients to refresh their tool catalogs.
   The instances and Agent pages now expose knowledge controls. Configure optional
   upload/OSS settings following [administrator onboarding](polarrag-onboarding.md).

The first restart assembles the APIs, MCP tools, catalog synchronization and upload
cleanup workers. Saving configuration does not load or unload Python modules in a
running process. `ENABLED` confirms configuration activation; upstream requests
can still fail if the remote service later becomes unavailable.

A single self-hosted replica confirms itself after startup validation. For multiple
replicas, restart every replica, verify the complete deployment topology, and use
**Confirm all replicas** only when all expected replicas are present at the target
revision. Heartbeat expiry is not proof that a disconnected process has stopped.
Self-hosted confirmation is an operator action, not a fleet discovery guarantee.

## Disable safely

Choose **Start disabling**. This closes admission for new knowledge work while
allowing upload cleanup to finish. The card shows in-flight operations, unresolved
uploads, cleanup records, and running catalog synchronizations. When all are zero,
choose **Save disabled state**, then restart PAS. The state becomes
`DISABLED`. Data, connections and credentials remain stored for later reactivation.

If cleanup is blocked, leave the service running and resolve the reported work.
To retry a catalog synchronization, cancel the drain, retry the synchronization,
then drain again. Do not delete upload cleanup records to bypass a failed external
operation. If a process died mid-operation, its admission record deliberately has
no automatic expiry: first stop/fence that process, inspect the upstream outcome
and reconcile its upload/cleanup or catalog state. An operator may then repair
only that verified stale operation in `feature.knowledge.admission`, using a
backup and a conditional `config_version` update. Never clear the whole record or
change the active revision to force success. The records are reserved operational
state, not editable feature configuration.

A failed startup verification leaves MySQL available and reports `FAILED`. Correct
the upstream identity/connection, reopen the wizard to save and verify a corrected
connection, or save the disabled configuration, then restart. Configuration rollback
creates a new revision; it does not rewind revision numbers or delete knowledge.

## Managed deployments

Administrators configure knowledge in **Advanced settings → Features → Knowledge**
in PAS. Use the same connection, space and protected-read verification wizard as
self-hosted deployments, then choose **Save configuration**. Saving creates the
desired revision and shows `PENDING_RESTART`; it does not submit a restart or admit
knowledge requests.

Apply the revision using **Restart instance** in the control-plane console. The
controller captures the desired revision and enabled state from every replica
before starting the serial restart and retains this target in the workflow's
initialization output. Each replica must load and validate that exact target.
After verifying the physical topology and all current replica IDs, the controller
confirms activation. `ACTIVATING` means this final confirmation is still pending;
wait for `ENABLED` before reconnecting MCP clients. PAS browser users cannot
confirm a managed fleet, even when they are administrators.

For disable, finish the drain and save the disabled configuration in PAS before
starting the instance restart. Do not change the desired configuration during a
rollout. A changed revision or failed replica stops completion; repair and retry
the task. For a configuration rollback, save a new revision and start a new
restart after resolving the previous task. MySQL availability depends on remaining
serving capacity: the ordinary knowledge rollout requires a second healthy serving
replica; a single replica needs a separately planned maintenance procedure.

Deploy a controller that discovers knowledge configuration during ordinary instance
restart **before** deploying this PAS console change. An older controller that
restarts processes without knowledge verification/confirmation can leave the
feature in `ACTIVATING`. Existing tasks keep their recorded initialization target;
create a new restart task to apply configuration saved after that initialization.
The console does not require a new restart API, reverse callback, or credentials
for PAS to invoke the control plane.

The management listener continues to expose authenticated, instance/generation-scoped
`POST /api/internal/v1/features/knowledge` with protocol version `1`, `target`,
`action`, and `parameters`. It requires an already-bound managed identity.
Supported actions remain `status`, `options`, `save_connection`, `check_connection`,
`prepare_space`, `test`, `config`, `drain`, `cancel_drain`, and `confirm_activation`.
`config` accepts only the `knowledge` module and `describe`, `save_draft`, `validate`,
`activate`. Public configuration requires an administrator and browser CSRF
protection; direct `reset`, `skip` and `disable` commands cannot bypass the managed
feature workflow. The administrator initialization API remains restricted to
account setup. Enabling knowledge never grants access to resources or changes ACLs.

## State and compatibility

| State | Meaning |
| --- | --- |
| `DISABLED` | No knowledge APIs, tools or workers are assembled. |
| `PENDING_RESTART` | Saved and loaded revisions differ. |
| `ACTIVATING` | Prepared replicas await deployment-wide confirmation. |
| `ENABLED` | The loaded revision is admitted. |
| `DRAINING` | New work is closed; existing work must finish. |
| `FAILED` | Startup verification failed; inspect the sanitized error code. |

Old knowledge HTTP URLs return `503 KNOWLEDGE_NOT_ENABLED` when disabled instead
of a misleading HTML page. MySQL routes remain available. Feature state lives in
`module.knowledge` and reserved `feature.knowledge.*` records. Do not edit these
records through a generic configuration editor.

See [upgrade and rollback](../deployment/upgrade-and-rollback.md) for schema
compatibility and [guided configuration](../configuration/guided-configuration.md)
for the configuration protocol.
