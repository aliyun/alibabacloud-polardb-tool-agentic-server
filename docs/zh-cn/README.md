# 文档

[English](../en/README.md) | **简体中文**

Alibaba Cloud PolarDB Tool Agentic Server 的用户与运维文档。

## 初始化

- [初始化设置](setup/initial-setup.md)：配置元数据库和根密钥、执行迁移、完成
  首次接管，在本地、Docker 和 Kubernetes 多副本环境交付 bootstrap token，
  并恢复丢失或过期的 token。

## 配置

- [引导式模块化配置](configuration/guided-configuration.md)：在 UI 或 CLI 中
  配置可选模块，对声明式变更执行 dry run，激活依赖感知配置，并导出脱敏设置。

## 管理

- [用户与部门](administration/users-and-departments.md)：人类身份、组织、
  实例访问和安全生命周期操作。
- [认证](administration/authentication.md)：首次接管、内置登录、可选 SSO、
  Session 和 Agent 身份隔离。
- [Agent 与 Token](administration/agents-and-tokens.md)：Agent 创建、Token
  生命周期、访问绑定和复查。
- [审计与安全](administration/audit-and-security.md)：审计范围、密钥边界、
  破坏性操作和保留。

## Agent

- [连接 MCP 客户端](agents/connect-mcp-client.md)：生成的 JSON、网络/TLS
  要求、重连行为和诊断。
- [工具参考](agents/tool-reference.md)：动态工具目录、必填参数、标识、分页和
  可操作错误。
- [SQL 访问模型](agents/sql-access-model.md)：可选 SQL 代理、能力、资源自洽
  和后端权限限制。

## 部署

- [生产部署前提](deployment/prerequisites.md)：支持的平台、元数据库、根密钥
  管理、可写目录与镜像仓库访问要求。
- [Docker Compose](deployment/docker-compose.md)：使用固定 MySQL、一次性迁移、
  备份和升级流程的单机部署。
- [Kubernetes 与 Helm](deployment/kubernetes-helm.md)：安全的多副本部署、
  迁移 Hook、渲染清单流程与升级。
- [ACK 与 PolarDB](deployment/ack-polardb.md)：推荐的 PolarDB MySQL 8.0
  元数据库网络位置。
- [生产网络](deployment/networking.md)：公网/VPC OpenAPI、数据库路由、
  Ingress 与 TLS 责任边界。
- [离线与私有镜像仓库安装](deployment/offline-installation.md)：验证 Release
  资产、加载按架构拆分的镜像，并镜像到客户自有仓库。
- [升级与回滚](deployment/upgrade-and-rollback.md)：迁移优先的升级、备份、
  验证和 Schema 回滚限制。

## 参考

- [CLI 参考](reference/cli.md)：安全地检查和迁移元数据库 Schema、启动服务，
  以及执行引导式配置。
- [配置模块](reference/configuration-modules.md)：模块目录、状态、依赖验证、
  外部检查和密钥行为。
- [REST API](reference/rest-api.md)：认证、资源、配置命令和生成的 OpenAPI。
- [兼容性](reference/compatibility.md)：支持的运行时、数据库迁移规则、API
  策略和预发布版本规则。
- [发布流程](reference/release-process.md)：受保护的 tag 工作流、Draft
  检查、GHCR 可见性、attestation 和不可变策略。

## 数据库实例

- [实例注册](database-instances/registration.md)：连接四元组、后端 Pod
  测试、多租预检、编辑和轮换。
- [数据库实例访问与供应](database-instances/access-and-provisioning.md)：在 UI
  中注册普通和多租户 PolarDB MySQL 实例、管理凭证和供应后端、签发 Agent
  Token、授权访问、调用四个数据库实例 Tool，以及运维清理流程。
- [多租户供应](database-instances/multitenant-provisioning.md)：前提、后端
  策略、Agent 供应和恢复。

## 运维

- [健康与就绪](operations/health-and-readiness.md)：存活、配置收敛、探针和
  告警。
- [日志与可观测性](operations/logs-and-observability.md)：启动、运行时信号、
  脱敏和保留。
- [备份与恢复](operations/backup-and-restore.md)：数据库/根密钥恢复集、
  恢复限制和验证。
- [凭证与密钥轮换](operations/credential-and-key-rotation.md)：数据库、
  Agent、云、SSO 和根密钥流程。
- [故障排查](operations/troubleshooting.md)：启动、就绪、网络、MCP/SQL 和
  供应诊断。

## 开发

- [项目 README](../../README_zh-CN.md)：项目简介、本地测试配置、管理流程和
  开发检查。
- [贡献与翻译](../../CONTRIBUTING.md)：Pull Request 检查和多语言文档规范。

公开文档只描述已经交付的行为。设计提案、实施计划、客户专属记录、凭证和
私有链接不应进入本目录。
