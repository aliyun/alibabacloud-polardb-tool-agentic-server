# Feature usage 4: resource pool and instances

**English** | [简体中文](../../zh-cn/getting-started/resource-pool.md)

The resource pool pre-provisions instances up to a target capacity so user
requests hit ready instances and wait less. This page sets the target
capacity, triggers replenishment, and manages instances in the pool.

## Set the target capacity

In system settings, set the resource pool target capacity (`target_size`) to a
value greater than 0. The replenishment loop pre-provisions instances up to the
target; nothing is pre-provisioned when the target is 0.

<p align="center">
  <img src="../../zh-cn/getting-started/images/pool-target-size.png" alt="resource_pool target capacity settings" width="820">
</p>

## Trigger replenishment

On the pool page, click **Replenish** to trigger a replenishment run
immediately. The page shows counts of target, available, creating, and failed.

<p align="center">
  <img src="../../zh-cn/getting-started/images/pool-status.png" alt="Pool status and replenishment" width="820">
</p>

## View and manage instances

Instances in the pool are shown by status:

- `active`: available instances that can be assigned or removed from the pool.
- `creating`: instances being created.
- `failed`: instances that failed to create.

For failed instances, and for placeholder instances stuck under the
`pool-pending` prefix because their creation task was interrupted (for example
by a process restart), you can remove them directly on the page.

## Automatic cleanup of placeholders

The replenishment loop automatically cleans up `pool-pending` placeholder rows
that have been stuck and cannot make progress, freeing replenishment slots so
new instances continue to be created.

## Learn more

For the full explanation of instance registration, access, and provisioning,
see
[Database instance access and provisioning](../database-instances/access-and-provisioning.md).

You have now completed the end-to-end flow from resource preparation to feature
usage.
