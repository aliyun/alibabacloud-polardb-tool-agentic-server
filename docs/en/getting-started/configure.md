# Feature usage 1: guided configuration

**English** | [简体中文](../../zh-cn/getting-started/configure.md)

After claiming ownership, open the console to configure cloud credentials and
the Service runtime policy. This page covers the service-level configuration
needed before an auto-provisioning pool can purchase clusters.

## Open the configuration page

Signed in as an administrator, open **Service Configuration**
(`/settings/configuration`). Optional modules can be configured step by step;
each change creates a draft first and is activated only after validation
passes.

<p align="center">
  <img src="../../zh-cn/getting-started/images/configuration-modules.png" alt="Configuration module list" width="820">
</p>

## Configure aliyun_access

Fill in the Alibaba Cloud credentials and region:

- `access_key_id` / `access_key_secret`: a RAM credential with PolarDB cluster
  management permissions.
- `region_id`: the target region.
- `endpoint_network`: choose `public` or `vpc` to decide which network reaches
  the PolarDB OpenAPI. If PAS runs in the same VPC as PolarDB, prefer `vpc`.

<p align="center">
  <img src="../../zh-cn/getting-started/images/configure-aliyun-access.png" alt="aliyun_access configuration form" width="820">
</p>

## Review Service runtime policy

Open **Service runtime policy** and enable the auto-provisioning worker. The
pool wizard links to `/settings/configuration?module=runtime_policy` when this
worker is not running. Fixed `CreateDBCluster` values are not configured here:
the server-owned `agentic-dedicated-mysql` profile is their only source.

## Create an auto-provisioning pool

Pool configuration is not a service module. Open **Pool**, create an
**Auto-provisioning pool (AgenticDB Dedicated)**, and enter its placement and
capacity:

- `region_id` and `zone_id` are required.
- Both `vpc_id` and `vswitch_id` are required. PAS cannot automatically
  determine the VPC of the ECS instance, container, or Kubernetes environment
  where it runs, so it does not use the Alibaba Cloud account's default VPC.
- Specify a VPC reachable from PAS and choose a VSwitch in that VPC and target
  zone. PAS and the auto-provisioning pool normally use the same VPC. If they use
  different VPCs, establish connectivity first, for example through Cloud
  Enterprise Network or VPC peering.
- Follow the regional VPC console link shown by the form to copy the VPC and
  VSwitch identifiers. This is manual by design and does not require extra RAM
  permissions to enumerate network resources.
- Set a target size, hard member limit, purchase budget, permission revision,
  reclaim policy, and cooldown. PAS prepares capacity toward the target only
  after the auto-provisioning worker, Alibaba Cloud identity, internal purchase
  profile, and default permission revision are ready.

The page may list multiple auto-provisioning pools. Bind a
primary and optional ordered fallback pools on the Agent detail page.

## Learn more

For module dependencies, declarative apply, export, and reload behavior, see
[Guided modular configuration](../configuration/guided-configuration.md).
For pool fields, readiness blockers, routing, and cold creation, see
[Auto-provisioning pools](../database-instances/dedicated-hot-pools.md).

Next: [Feature usage 2: register a database instance](./register-instance.md).
