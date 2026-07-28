# 快速上手教程总览

[English](../../en/getting-started/overview.md) | **简体中文**

本教程带你从零开始，在一台阿里云 ECS 上完成 PolarDB Tool Agentic Server
（下称 PAS）的资源准备、部署与核心功能体验。教程按顺序编排，建议从上到下
逐页操作，关键界面已预留配图位置。

## 适用读者

- 首次试用 PAS、希望在单台服务器上快速跑通端到端流程的管理员。
- 已有阿里云账号，能够购买 ECS 与 PolarDB MySQL 等云资源。

本教程主打单台 ECS + Docker Compose 的部署方式。多副本生产部署请在完成本
教程后参阅
[Kubernetes 部署指南](../deployment/kubernetes-helm.md)。

## 端到端路线图

1. 准备云资源：一台 ECS 与一套作为元数据库的 PolarDB MySQL。
   - 注意：ECS 需要具备公网访问能力，用于下载部署文件与 Docker 镜像。
   - 注意：ECS 与 PolarDB MySQL 需位于同一 VPC，以便内网互通。
2. 在 ECS 上用 Docker Compose 部署 PAS 并完成 Owner 认领。
3. 通过引导式配置接入阿里云凭证与购买规格。
4. 注册已有的 PolarDB 集群，供后续授权给 Agent 使用。
5. 创建 Agent、签发 Token、授权实例访问，并连接 MCP 客户端调用工具。
6. 配置资源池，预建并管理实例。

## 前置条件

- 一个具备云资源购买权限的阿里云账号。
- 一对拥有 PolarDB 集群管理权限的 RAM AccessKey（后续引导式配置使用）。
- 本地具备 SSH 客户端，用于登录 ECS。

## 教程导航

- [资源要求](./cloud-resources.md)：购买 ECS 与 PolarDB MySQL 元数据库。
- [部署（单台 ECS + Docker Compose）](./deploy-compose.md)：部署并完成接管。
- [功能使用①：引导式配置](./configure.md)：配置云凭证与购买规格。
- [功能使用②：注册数据库实例](./register-instance.md)：注册已有集群。
- [功能使用③：Agent、Token 与 MCP](./agents-and-mcp.md)：授权并调用工具。
- [功能使用④：资源池与实例](./resource-pool.md)：预建与管理实例。
