# Alibaba Cloud PolarDB Tool Agentic Server

[English](README.md) | **简体中文**

这是一个面向 PolarDB MySQL 的开源模型上下文协议（MCP）网关，为用户和独立
管理的 Agent 提供经过身份认证、可审计的数据库发现、受控 SQL 操作、分支
操作和持久逻辑数据库资源。

## 功能

- 提供支持 OAuth 和内置认证的 Streamable HTTP MCP 服务。
- 支持 PolarDB 实例发现、路由和基于能力的访问控制。
- 为 User 提供受行数限制、危险操作确认、限流和审计保护的 SQL 执行。
- 通过 Web 控制台管理独立 Agent 身份及其一对一 API Token。
- 支持一个 Agent 直接访问多个已注册的 PolarDB MySQL 实例。
- 提供存储在数据库中的多租户供应后端，以及健康、容量、排空、清理和恢复控制。
- 提供四个按授权动态展示的实例 Tool：`list_db_instances`、
  `create_db_instance`、`describe_db_instance` 和 `delete_db_instance`。
- 提供 FastAPI 后端和 React/Vite 管理控制台。

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 或更高版本，仅开发 Web 控制台时需要

### 启动后端

```bash
uv sync --extra dev

export PAS_DATABASE_URL='sqlite+aiosqlite:///data/polardb_agentic.db'
export PAS_ENCRYPTION_KEY="$(
  python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
)"

uv run alembic upgrade head
uv run python -m server
```

后端监听 `http://localhost:18760`。`PAS_DATABASE_URL` 和
`PAS_ENCRYPTION_KEY` 是服务仅有的两个启动配置。生产环境应通过 Kubernetes
Secret 或权限受限的挂载文件提供并独立备份根密钥，同时使用持久化的 MySQL
或 PostgreSQL 元数据库。

### 启动 Web 控制台

```bash
cd web
npm install
npm run dev
```

打开 `http://localhost:18761`。元数据库为空时，初始化控制台会要求输入一次性
bootstrap token，并引导创建首个管理员。SSO、阿里云访问、购买和资源池等
可选模块均可跳过，以后再配置。

只能使用终端的部署环境可采用交互式或声明式流程：

```bash
pas config init
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
pas config export --file effective.yaml
```

bootstrap token 交付、Docker 与 Kubernetes 命令和恢复方式详见
[初始化设置](docs/zh-cn/setup/initial-setup.md)。模块依赖、密钥引用和配置
流程详见[引导式模块化配置](docs/zh-cn/configuration/guided-configuration.md)。

生产化单机试用请使用受支持的
[Docker Compose 部署](docs/zh-cn/deployment/docker-compose.md)，它会启动
固定版本的 MySQL 8.0、执行一次迁移，然后启动服务。Kubernetes 运维人员请先
阅读[生产部署前提](docs/zh-cn/deployment/prerequisites.md)和
[Helm 部署指南](docs/zh-cn/deployment/kubernetes-helm.md)。

## 管理流程

Web 控制台是运行时实例访问配置的事实来源：

1. 在 **Instances** 中注册 `polardb_mysql` 物理实例，拓扑选择
   `single_tenant` 或 `multitenant`，并提供可访问的 host、port、用户名和
   密码。可以填写可选的 Usage 用途说明，帮助 Agent 识别实例用途。注册前使用
   **Test Connection** 执行 `SELECT 1`。分配模式固定为 `registered`。注册
   后可在详情页使用 **Edit Instance** 修改显示名称、用途、地域、Host 或
   Port。
2. 注册时会创建加密的初始 `direct_access` 凭证；`multitenant` 实例则创建
   `provisioning_admin` 凭证。需要创建多租户逻辑数据库时，再配置供应后端的
   容量、CPU 范围和 DDL 并发数。
3. 在 **Agents** 中创建 Agent 并安全保存 Token。管理员可直接查看当前有效
   Token 和 MCP 客户端配置，也可以重新生成或吊销 Token；重新生成后，旧
   Token 立即失效。
4. 在 Agent 统一的 **Instance access** 中只授予所需的直连元数据或 SQL
   能力。对于供应后端活跃且健康的多租户实例，可按需勾选
   **Create managed databases**；该能力默认关闭，也可以不授予直连 SQL
   访问而单独启用。
5. 在 **Users** 中，管理员还可以调整每个 User 的实例凭证、`readonly` 或
   `readwrite` 权限和能力。

供应后端和凭证存储在元数据库中。不再通过部署时环境变量指定唯一多租户实例，
调整绑定也不需要重新部署服务。

## 数据库实例 Tool

授权后的 Tool 列表会随认证主体的绑定和自有资源动态变化：

- `list_db_instances` 使用游标分页和过滤条件，列出已授权的物理实例和未删除
  资源，并返回可选的 `usage` 用途说明。
- `create_db_instance(client_token, db_type, name?)` 仅供 Agent 使用，当前接受
  `db_type="polardb_mysql"`，通过已授权的多租户后端创建持久逻辑数据库。
- `describe_db_instance(db_instance_id)` 返回已授权的元数据；只有调用方拥有
  凭证读取能力且资源已就绪时，才会包含连接凭证。`usage` 对注册物理实例返回
  已填写内容；未填写或供应的逻辑资源返回 `null`。
- `delete_db_instance(db_instance_id)` 仅供 Agent 删除自己创建的供应资源。
- 有效直连绑定会开放 `run_sql`、`run_sql_transaction` 和
  `describe_schema`。Agent 必须把 `list_db_instances` 返回的稳定实例 UUID
  作为 `instance_id` 传入；展示名称不是标识符。

`client_token` 是单个 Agent 范围内永久占用的幂等键。使用相同规范化参数重试
会返回原资源，即使资源已经 `DELETED`；使用不同参数会返回幂等冲突。本版本
不提供资源自动过期，请显式调用 `delete_db_instance`。

完整 UI 流程、安全模型、Tool 示例和生命周期参见
[数据库实例访问与供应指南](docs/zh-cn/database-instances/access-and-provisioning.md)。

## 文档

- [English documentation](docs/en/README.md)
- [简体中文文档](docs/zh-cn/README.md)
- [快速上手教程](docs/zh-cn/getting-started/overview.md)
- [初始化设置](docs/zh-cn/setup/initial-setup.md)
- [引导式模块化配置](docs/zh-cn/configuration/guided-configuration.md)
- [数据库实例访问与供应](docs/zh-cn/database-instances/access-and-provisioning.md)
- [Docker Compose 部署](docs/zh-cn/deployment/docker-compose.md)
- [Kubernetes 与 Helm 部署](docs/zh-cn/deployment/kubernetes-helm.md)
- [贡献与翻译指南](CONTRIBUTING.md)
- [环境变量示例](.env.example)

## 开发检查

```bash
uv run --extra dev ruff check .
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: \
PAS_ENCRYPTION_KEY=MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE= \
uv run --extra dev pytest

cd web
npm test -- --run
npm run lint
npm run build
```

只有当 `PAS_PERF_*` 变量明确指向使用 MySQL 或 PostgreSQL 元数据库的真实
VPC 部署时，才会运行性能测试。

## 参与贡献

欢迎提交贡献。发起 Pull Request 前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)。英文文档是技术行为的规范来源；同一个
Pull Request 中应同步更新对应的简体中文页面。

## 许可证

项目采用 [Apache License 2.0](LICENSE)，归属信息参见 [NOTICE](NOTICE)。
