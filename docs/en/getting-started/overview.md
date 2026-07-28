# Getting started overview

**English** | [简体中文](../../zh-cn/getting-started/overview.md)

This tutorial takes you from zero to a working PolarDB Tool Agentic Server
(PAS) on a single Alibaba Cloud ECS: cloud resources, deployment, and a
walkthrough of the core features. Follow the pages top to bottom; key screens
have placeholders reserved for figures.

## Who this is for

- Administrators trying PAS for the first time who want an end-to-end run on a
  single server.
- Users with an Alibaba Cloud account who can buy ECS and PolarDB MySQL
  resources.

This tutorial features the single ECS + Docker Compose path. For a
multi-replica production deployment, see the
[Kubernetes deployment guide](../deployment/kubernetes-helm.md) after
finishing this tutorial.

## End-to-end roadmap

1. Prepare cloud resources: one ECS and one PolarDB MySQL as the metadata
   database.
   - Note: the ECS needs public network access to download the deployment
     files and the Docker image.
   - Note: the ECS and the PolarDB MySQL must share the same VPC for private
     connectivity.
2. Deploy PAS on the ECS with Docker Compose and claim ownership.
3. Configure cloud credentials and purchase settings through guided
   configuration.
4. Register an existing PolarDB cluster for later Agent authorization.
5. Create an Agent, issue a Token, grant instance access, and connect an MCP
   client to call tools.
6. Configure the resource pool to pre-provision and manage instances.

## Prerequisites

- An Alibaba Cloud account allowed to purchase cloud resources.
- A RAM AccessKey pair with PolarDB cluster management permissions (used later
  in guided configuration).
- An SSH client to log in to the ECS.

## Tutorial navigation

- [Resource requirements](./cloud-resources.md): buy the ECS and the PolarDB
  MySQL metadata database.
- [Deployment (single ECS + Docker Compose)](./deploy-compose.md): deploy and
  claim ownership.
- [Feature usage 1: guided configuration](./configure.md): configure cloud
  credentials and purchase settings.
- [Feature usage 2: register a database instance](./register-instance.md):
  register an existing cluster.
- [Feature usage 3: Agent, Token, and MCP](./agents-and-mcp.md): authorize and
  call tools.
- [Feature usage 4: resource pool and instances](./resource-pool.md):
  pre-provision and manage instances.
