# Documentation

**English** | [简体中文](../zh-cn/README.md)

User and operator documentation for Alibaba Cloud PolarDB Tool Agentic Server.

## Getting started

- [Getting started overview](getting-started/overview.md): end-to-end roadmap
  for a single ECS trial, from cloud resources to feature usage.
- [Resource requirements](getting-started/cloud-resources.md): buy an ECS and a
  PolarDB MySQL metadata database, and build the connection string.
- [Deployment (single ECS + Docker Compose)](getting-started/deploy-compose.md):
  install Docker, prepare `.env`, migrate, start, and claim ownership.
- [Feature usage: guided configuration](getting-started/configure.md): set cloud
  credentials, purchase settings, and pool network placement.
- [Feature usage: register a database instance](getting-started/register-instance.md):
  register an existing PolarDB cluster and verify connectivity.
- [Feature usage: Agent, Token, and MCP](getting-started/agents-and-mcp.md):
  create an Agent, issue a Token, grant access, and call the tools.
- [Feature usage: resource pool and instances](getting-started/resource-pool.md):
  set target capacity, replenish, and manage pooled instances.

## Setup

- [Initial setup](setup/initial-setup.md): configure the metadata database and
  root key, run migrations, claim first ownership, deliver bootstrap tokens in
  local, Docker, and multi-replica Kubernetes environments, and recover a lost
  or expired token.

## Configuration

- [Guided modular configuration](configuration/guided-configuration.md):
  configure optional modules in the UI or CLI, dry-run declarative changes,
  activate dependency-aware configuration, and export redacted settings.

## Administration

- [Users and departments](administration/users-and-departments.md): human
  identities, organization, instance access, and safe lifecycle operations.
- [Authentication](administration/authentication.md): bootstrap, built-in
  login, optional SSO, sessions, and Agent identity separation.
- [Agents and tokens](administration/agents-and-tokens.md): Agent creation,
  Token lifecycle, access bindings, and review.
- [Audit and security](administration/audit-and-security.md): audit scope,
  secret boundaries, destructive operations, and retention.

## Agents

- [Connect an MCP client](agents/connect-mcp-client.md): generated JSON,
  network/TLS requirements, reconnect behavior, and diagnosis.
- [Tool reference](agents/tool-reference.md): dynamic tool catalog, required
  parameters, identifiers, pagination, and actionable errors.
- [SQL access model](agents/sql-access-model.md): optional SQL proxy,
  capabilities, resource coherence, and backend permission limits.

## Deployment

- [Agent-assisted single-host deployment](deployment/agent-assisted-deployment.md):
  explicitly invoked Codex, Claude Code, Cursor, and compatible Agent Skills
  workflows pinned to PAS 0.0.5.
- [Production prerequisites](deployment/prerequisites.md): supported
  platforms, metadata database, root-key handling, writable paths, and
  registry access.
- [Docker Compose](deployment/docker-compose.md): supported single-host
  deployment with pinned MySQL, one-shot migrations, backups, and upgrades.
- [Kubernetes with Helm](deployment/kubernetes-helm.md): secure multi-replica
  deployment, migration hooks, rendered-manifest procedure, and upgrades.
- [ACK with PolarDB](deployment/ack-polardb.md): recommended PolarDB MySQL 8.0
  metadata database placement.
- [Production networking](deployment/networking.md): public/VPC OpenAPI,
  database routing, Ingress, and TLS ownership.
- [Offline and private-registry installation](deployment/offline-installation.md):
  verify release assets, load architecture-specific images, and mirror them
  into a customer-controlled registry.
- [Upgrade and rollback](deployment/upgrade-and-rollback.md): migration-first
  upgrades, backups, validation, and schema rollback limits.

## Reference

- [CLI reference](reference/cli.md): check and migrate the metadata schema,
  start the service, and operate guided configuration safely.
- [Configuration modules](reference/configuration-modules.md): module catalog,
  states, dependency validation, external checks, and secret behavior.
- [REST API](reference/rest-api.md): authentication, resources, configuration
  commands, and generated OpenAPI.
- [Compatibility](reference/compatibility.md): supported runtimes, database
  migration rules, API policy, and pre-release versioning.
- [Release process](reference/release-process.md): protected tag workflow,
  Draft inspection, GHCR visibility, attestations, and immutability.

## Database instances

- [Registration](database-instances/registration.md): connection tuple,
  backend-Pod testing, multitenant preflight, editing, and rotation.
- [Database instance access and provisioning](database-instances/access-and-provisioning.md):
  register ordinary and multitenant PolarDB MySQL instances, manage
  credentials and provisioning backends in the UI, issue Agent Tokens, grant
  access, call the four database instance Tools, and operate cleanup.
- [Multitenant provisioning](database-instances/multitenant-provisioning.md):
  prerequisites, backend policy, Agent provisioning, and recovery.

## Operations

- [Health and readiness](operations/health-and-readiness.md): liveness,
  configuration convergence, probes, and alerting.
- [Logs and observability](operations/logs-and-observability.md): startup,
  runtime signals, redaction, and retention.
- [Backup and restore](operations/backup-and-restore.md): database/root-key
  recovery set, restore limits, and verification.
- [Credential and key rotation](operations/credential-and-key-rotation.md):
  database, Agent, cloud, SSO, and root-key procedures.
- [Troubleshooting](operations/troubleshooting.md): startup, readiness,
  networking, MCP/SQL, and provisioning diagnosis.

## Development

- [Project README](../../README.md): overview, local test setup, administration
  workflow, and development checks.
- [Contributing and translations](../../CONTRIBUTING.md): pull request checks
  and multilingual documentation rules.

Public documentation describes shipped behavior. Design proposals,
implementation plans, customer-specific records, credentials, and private
links do not belong in this documentation tree.
