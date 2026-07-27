# 数据库实例访问与供应

[English](../../en/database-instances/access-and-provisioning.md) | **简体中文**

分主题流程参见[实例注册](registration.md)和
[多租户供应](multitenant-provisioning.md)。

本文介绍管理员如何通过 Web 控制台注册 PolarDB MySQL 实例、管理凭证和供应
后端、向 User 和 Agent 授权，以及通过 MCP 运维持久逻辑数据库。

## 能力范围

本版本支持引擎为 `polardb_mysql`、拓扑为 `single_tenant` 或
`multitenant`、分配模式为 `registered` 的物理实例。普通实例和多租户实例
都可以通过直连绑定提供访问。只有 `polardb_mysql` + `multitenant` 实例可以
作为 `create_db_instance` 的供应后端。

生产 Agent 可以直连绑定多个物理实例。Agent 使用 `list_db_instances` 获取
可用实例和相应能力，通过 `describe_db_instance` 获取已授权连接信息，并通过
`run_sql` 将 SQL 发送到 MCP 服务的 SQL-over-HTTP 代理。服务使用已注册或
已绑定的 MySQL 账号连接选定后端。

供应得到的逻辑数据库会一直保留，直到显式调用 `delete_db_instance`。本版本
不会创建独享 PolarDB 集群，也不会按固定时间自动删除资源。

启用多租户供应前，请阅读
[PolarDB MySQL 版多租户管理官方文档](https://help.aliyun.com/zh/polardb/polardb-for-mysql/user-guide/multi-tenant-management-instructions)，
了解开通前提、支持范围、资源隔离和租户管理 SQL。

## 开始之前

MCP Server、每个 Agent 工作负载和每个已注册 PolarDB 地址之间必须具备 VPC
网络连通性。生产环境应：

- 通过内网 HTTPS Ingress 或负载均衡器发布 MCP 服务。
- 设置稳定的 `PAS_ENCRYPTION_KEY`，用于保护 Agent Token 密文和数据库凭证。
- 配置持久化 JWT 签名密钥。
- 多副本部署使用 MySQL 或 PostgreSQL 元数据库；SQLite 只用于单进程功能开发。
- 每个直连账号遵循最小权限原则，绝不向 Agent 暴露 `provisioning_admin`
  账号。

启动服务前执行迁移：

```bash
uv run alembic upgrade head
uv run python -m server
```

物理实例、凭证、供应后端和 Agent 绑定保存在元数据库中，并通过 UI 管理。
不存在指定唯一实例的环境设置，修改配置也不需要重新部署服务。

## 注册物理实例

以活跃管理员身份登录 Web 控制台，打开 **Instances → Register Instance**：

1. 输入 PolarDB 集群标识、便于识别的显示名称，以及可选的 **Usage** 用途
   说明。用途会去除首尾空白，最多 1024 个字符，用于帮助已授权 Agent 理解
   实例用途。
2. 引擎选择 `polardb_mysql`。
3. 普通实例的拓扑选择 `single_tenant`；已启用 PolarDB 多租户管理的实例选择
   `multitenant`。
4. 输入可选地域，以及必填的 VPC host、端口、用户名和密码。MySQL 端口默认
   为 `3306`。分配模式固定为 `registered`，表单不再展示该选项。
5. 选择 **Test Connection**。服务建立临时连接并执行 `SELECT 1`。对于
   `multitenant` 拓扑，还会要求 `enable_multi_tenant=ON`，并验证提交的
   用户名是逗号分隔的 `rds_kill_user_list` 中的精确成员。随后关闭连接，
   不保存本次提交的密码。
6. 选择 **Register Instance**。服务再次执行连接和拓扑专属检查，然后在同一
   事务中保存实例和加密凭证。任一检查失败时，两类记录都不会留下。

注册操作只是记录一个已有物理实例，不会创建 PolarDB 集群。单租户注册会创建
`direct_access` 凭证；多租户注册会创建 `provisioning_admin` 凭证。打开实例
详情页可查看用途、凭证、绑定和供应状态。选择 **Edit Instance**
可以修改显示名称、用途、地域、Host 或 Port；Cluster ID、引擎、拓扑和分配
模式保持不可变。修改 Host 或 Port 时，必须选择一个有效凭证；保存前由 PAS
后端 Pod 重新执行连接测试。只修改元数据不要求连接测试。移除未被引用的已注册
实例时，会一并移除它拥有的凭证；存在绑定或供应后端时，移除会被拒绝。

实例列表中的 **Provisioning** 表示自动数据库供应状态，而不是物理实例的通用
健康状态。`Not enabled` 表示尚未配置 Provisioning Backend；`Healthy` 或
`Unhealthy` 表示最近一次供应连接实时检查的结果。

MCP 使用选定的 MySQL 账号转发 SQL。Agent 最终能够访问哪些数据库、表以及
执行哪些操作，由该账号在 MySQL Backend 中的 GRANT 权限约束。MCP 服务不会
绕过或提升后端权限；应使用授权范围与 Agent 预期访问一致的最小权限账号。

多租户注册还可能返回以下前置校验错误：

- `MULTITENANT_DISABLED`：联系 PolarDB 支持开启
  `enable_multi_tenant`，重启集群后再重试。
- `MULTITENANT_ADMIN_REQUIRED`：改用受支持的高权限账号，其精确用户名必须
  出现在 `rds_kill_user_list` 中。
- `MULTITENANT_PREFLIGHT_FAILED`：服务无法读取必需的 PolarDB 参数，或参数
  查询结果格式无效。请检查集群兼容性，以及账号是否可以查看服务端参数。

如需为部门关联多租户容量，在 **Departments** 中展开部门并选择
**Bind Instance**，然后从列表中选择一个活跃、已注册的多租户实例。
Departments 不再填写连接信息或凭证；实例注册统一由 **Instances** 管理。
一个部门最多绑定一个多租户实例，同一个多租户实例可以服务多个部门。

## 配置凭证和供应后端

不同用途的凭证是相互独立的记录：

- `direct_access` 凭证是普通数据库账号，只会通过获得授权的直连绑定返回。
  声明能力为 `readonly` 或 `readwrite`。
- `provisioning_admin` 凭证必须具有 `admin` 能力，只能用于
  `polardb_mysql` + `multitenant` 实例。服务在内部使用它完成租户、账号、
  数据库、授权、验证和清理，任何 MCP Tool 都不会返回它。

注册时会创建与拓扑对应的初始凭证。多租户实例也可以添加用于普通 SQL 的
`direct_access` 账号，但必须与高权限 `provisioning_admin` 账号保持分离。
新增或修改凭证前选择 **Test Connection**；提交时 PAS 服务端会再次测试。
限定数据库的 Direct Access 凭证会连接到该数据库并执行 `SELECT 1`；
Provisioning Administrator 还会执行多租户前置校验。

选择 **Edit** 可以修改有效凭证或轮转密码，而无需改变凭证 ID 和现有绑定。
Username 或 Password 留空表示保留已存值。更新成功后凭证版本递增，旧连接池
会在下次使用时重新连接。明文会加密存储。管理员明确确认后才能查看凭证；
查看操作会被审计和限流，响应包含 `Cache-Control: no-store`。吊销后，该
凭证立即不能用于新的访问。

对于多租户实例，选择 **Configure Provisioning Backend**，并选择注册时创建
的 `provisioning_admin` 凭证。设置优先级、最大活跃资源数、资源 CPU 范围和
DDL 并发数。服务验证定义和连接后才会激活后端。

后端状态包括：

- `active`：可以接收新资源并执行后台任务。
- `draining`：不再接收新资源，已有任务和清理会继续。
- `disabled`：停止新的供应，仍保留清理和恢复能力。

可以将多个已注册多租户实例配置为独立后端。选择过程会综合授权、引擎、健康、
优先级和容量。

## 创建 Agent 并管理 Token

打开 **Agents → Create Agent**，设置唯一名称；如有需要，还可以设置最大
活跃资源数。Agent 与 Token 是一对一关系，每个 Agent 严格对应一条 Token
记录：

- 创建结果会显示新的 `pas_agent_...` Token。
- Agent 详情页会自动向已认证管理员展示当前有效的明文 Token。
- **MCP server URL** 展示控制台 origin 加 `/mcp` 后的地址。
- **Copy JSON configuration** 会复制包含 MCP 地址和 Token 的客户端配置，
  其中 server 名称默认为 Agent 名称。
- **Regenerate Token** 会替换 Token，旧 Token 立即停止认证。
- **Revoke Token** 会停止认证，直到重新生成 Token。

复制的配置格式如下：

```json
{
  "mcpServers": {
    "<agent-name>": {
      "url": "https://console.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <agent-token>"
      }
    }
  }
}
```

未设置 `expires_at` 时，Token 会一直有效，直到重新生成或吊销。如果配置了
过期时间，到期后 Token 不能用于认证，也不能再展示明文；Web 控制台会将其
标记为已过期，管理员必须使用 **Regenerate Token** 签发新的有效 Token。

服务使用 SHA-256 哈希完成认证，并为管理员展示保存加密密文。加载有效明文
会被审计和限流；秘密响应使用 `Cache-Control: no-store`，控制台只在 React
内存中保留该值。不要将 Token 写入 URL、日志、分析系统、浏览器存储或源码。
应将其保存在密钥管理服务中，并且只通过以下请求头发送：

```http
Authorization: Bearer <agent-token>
```

审计记录默认保留 180 天。清理任务每小时最多删除 500 条最早过期的记录。运维
人员可以调整 `sql_security.audit.retention_days`、
`cleanup_interval_seconds` 和 `cleanup_batch_size`；将清理间隔设为 `0`
可以禁用定时清理。

禁用 Agent 也会拒绝认证及其有效实例访问。

## 绑定实例访问

Agent 详情页对每个已注册物理实例只提供一个 **Instance access** 编辑器。一次
保存会在同一事务中更新直连和供应两部分；任一部分失败时，两部分都不会发生
变化。已有访问记录的实例不会再次出现在新增选择器中，即使某个底层部分已被
禁用；应编辑或移除现有记录，而不是重复创建。

直连访问需要选择属于该实例的活跃 `direct_access` 凭证、`readonly` 或
`readwrite` 权限，以及以下任意能力：

- `db_instance:list`
- `db_instance:describe`
- `db_instance:credentials:read`

首次选择直连凭证时会默认勾选 **Enable SQL over HTTP proxy**，从而提供
`run_sql`、`run_sql_transaction` 和 `describe_schema`。`readonly` 会保存
`sql:read`，`readwrite` 会保存 `sql:read` 和 `sql:write`。取消勾选可仅
保留实例清单或元数据访问。切换权限时会保留代理开关状态，并重新计算 SQL
能力。

对于活跃、已注册的 `polardb_mysql` + `multitenant` 实例，同一个编辑器还会
显示 **Create managed databases**。该选项默认不勾选，只有管理员明确选择后
才授予 `db_instance:create`。实例必须已经配置 `active` 且健康检查新鲜、结果
正常的供应后端，才能选择此项；否则编辑器会链接到实例详情页，供管理员配置或
修复后端。

系统允许仅供应访问：管理员可以只勾选 **Create managed databases**，无需
选择直连凭证或 SQL 权限。反之，仅直连访问不会授予创建能力。如果要在多租户
实例上同时开启两类能力，应先在实例详情页增加独立的 `direct_access` 凭证，
因为注册多租户实例时创建的是不可向 Agent 暴露的 `provisioning_admin`
账号。

取消 **Create managed databases** 会阻止新的 `create_db_instance` 请求，
但 Agent 已创建的逻辑数据库仍可列出、查询和删除。只要还存在未删除的自有
资源，就不能移除整条实例访问记录；此时 UI 会提供关闭数据库创建能力的操作。
应先删除自有资源，再移除聚合访问记录。

能力依赖会自动展开：读取凭证依赖查询和列表，查询依赖列表。请求的权限不能
超过凭证声明能力。每次调用时，服务都会取绑定、能力集合、凭证状态、Agent
状态和资源归属的交集。

选择 `readonly` 不会自动改写一个可写数据库账号的实际授权。若要由数据库
强制只读，应绑定一个 PolarDB 授权本身确实只读的账号。MCP 策略是附加防线；
MySQL Backend 仍然是数据库、对象和 SQL 权限的最终权威。

在 **Users** 中，管理员可以单独选择实例，修改 User 的凭证、权限、能力和
启用状态。系统自动创建的 SQL 访问仍保持 SQL-only，除非管理员显式授予实例
Tool 能力并选择有效直连凭证。

## Tool 授权与凭证规则

Tool 是否可见由当前访问关系决定：

- 拥有列表能力或未删除自有资源时显示 `list_db_instances`。
- 拥有查询能力或未删除自有资源时显示 `describe_db_instance`。
- Agent 至少存在一个已启用、凭证和 `sql:read` 能力仍有效的直连绑定，或拥有
  一个资源凭证有效的 `READY` 供应资源时，显示 `run_sql`、
  `run_sql_transaction` 和 `describe_schema`。取消
  **Enable SQL over HTTP proxy** 会从对应物理实例访问移除 SQL 能力。
  `sql:write` 决定哪些语句可以执行，不会增加另一套 Tool。
- 活跃 Agent 在某实例上拥有 `db_instance:create`，且该实例供应后端处于
  活跃、健康检查新鲜且结果正常状态时，会预先获得全部四个数据库实例 Tool，
  包括 `create_db_instance` 和 `delete_db_instance`。
- 当前没有创建能力的资源所有者，在仍拥有未删除资源时会获得列表、查询和
  删除 Tool；存在可用的 `READY` 资源时还会获得三个 SQL Tool。

返回的 Tool 列表应视为稳定的能力发现提示，不代表操作必然成功。容量不会让
`create_db_instance` 从列表中消失；后端已满时，调用会返回
`CAPACITY_EXHAUSTED`。每次 Tool 调用都会根据适用于该操作的当前绑定、后端、
归属、健康和容量状态重新鉴权。许多 MCP 客户端会缓存首次 `tools/list`；本
版本不依赖 `notifications/tools/list_changed`。管理员修改绑定、后端状态、
Agent 状态或资源状态后，应让客户端重新连接或再次调用 `tools/list`。

`list_db_instances` 返回安全元数据、权限、能力名称和 `usage` 字段，
`describe_db_instance` 返回相同的用途元数据。已注册物理实例返回管理员填写
的内容；未填写用途的物理实例和供应的逻辑资源返回 `null`。列表默认不返回
`DELETED` 资源。只有 `db_instance:credentials:read` 生效且凭证仍然有效时，
`describe_db_instance` 才会包含直连凭证。供应资源只有处于 `READY` 且资源
凭证可用时才返回凭证、`run_sql_read` 和 `run_sql_write` 能力。
`CREATING`、`FAILED`、`DELETING` 和 `DELETE_FAILED` 生命周期记录仍会展示，
但不能执行 SQL。未授权实例与不存在实例使用相同错误，避免枚举标识。

对于已注册物理实例，`db_instance_id` 是该实例在元数据库注册记录中的稳定、
不透明 UUID；自有供应资源使用稳定的 `dbi-*` 资源 ID。二者都不是绑定 ID，
也不会在每次列表请求时重新生成。`name` 只用于展示，可以修改且不要求唯一。

对于 Agent，`list_db_instances` 返回的 `ACTIVE` 或 `READY` 条目只要包含
`run_sql_read`，其 `db_instance_id` 就可以原样传给 `run_sql`、
`run_sql_transaction` 和 `describe_schema`。供应资源始终使用系统生成的
账号和数据库；`database` 参数必须省略或等于该资源数据库，不能借此选择承载
它的多租户物理实例上的其他数据库。

Agent 调用 SQL Tool 时必须始终提供 `instance_id`，不能使用
`set_default_instance` 或 Branch Tool。每次调用都会重新校验指定绑定或资源
的归属、状态、能力和凭证。

## MCP 调用

服务使用基于 Streamable HTTP 的 JSON-RPC：

```text
POST /mcp
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer <agent-token>
```

使用可选的 `cursor`、`limit`（1–200）、`db_type`、`source` 和 `status`
过滤条件列出已授权实例：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "list_db_instances",
    "arguments": {
      "limit": 50,
      "db_type": "polardb_mysql"
    }
  }
}
```

执行任一可用返回实例的 SQL 时，把其 `db_instance_id` 作为 `instance_id`
原样使用：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "run_sql",
    "arguments": {
      "instance_id": "dbi-00000000000000000000000000000000",
      "sql": "SELECT 1"
    }
  }
}
```

`run_sql_transaction` 和 `describe_schema` 使用同一个显式
`instance_id`。`readonly` 绑定允许只读语句和 Schema 查询；`readwrite`
绑定或 `READY` 供应资源可以执行写操作，但仍受现有 SQL 安全和确认策略约束。
仍在创建的资源会提示调用者等待 `READY` 后重试；创建失败或正在删除的资源会
提示查询详情或选择另一个列表资源。在所有情况下，所选 MySQL 账号的后端授权
都是数据库、对象和操作的最终权限边界，本服务不会绕过或提升这些权限。

创建持久逻辑数据库：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "create_db_instance",
    "arguments": {
      "client_token": "production-agent-2026-001",
      "db_type": "polardb_mysql",
      "name": "orders-reporting"
    }
  }
}
```

`client_token` 长度为 1–128，只允许 ASCII 字母、数字、`.`、`_`、`:` 和
`-`。它会在该 Agent 范围内永久绑定到规范化后的 `db_type` 和可选 `name`。
相同请求重试会返回同一个 `db_instance_id`，即使已进入 `DELETED`；使用相同
键提交不同请求会返回 `IDEMPOTENCY_CONFLICT`。

查询已授权物理实例或自有逻辑数据库：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "describe_db_instance",
    "arguments": {
      "db_instance_id": "dbi-..."
    }
  }
}
```

收到 `RATE_LIMITED` 后，应遵守 `retry_after_seconds`。不要记录包含 `host`、
`database`、`username` 或 `password` 的响应。

删除自有逻辑数据库：

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "delete_db_instance",
    "arguments": {
      "db_instance_id": "dbi-..."
    }
  }
}
```

数据库实例 Tool 通过 MCP 提供，不存在并行的公开 REST 生命周期 API。

### 常见 Tool 错误

- `INVALID_CLIENT_TOKEN`：`create_db_instance` 收到的键长度或字符不合法。
- `IDEMPOTENCY_CONFLICT`：Agent 使用相同键提交了不同创建参数。
- `UNSUPPORTED_DB_TYPE`：创建或列表请求指定了不支持的数据库类型。
- `NO_PROVISIONING_BACKEND`：当前没有已授权、活跃且健康的后端可接受 Agent
  请求。
- `CAPACITY_EXHAUSTED`：权威容量预留发现所有后端或 Agent 容量均不可用。
- `DB_INSTANCE_NOT_FOUND`：查询或删除找不到该主体可见的实例。
- `INVALID_CURSOR`：列表游标无效、已过期或与当前过滤条件不匹配。
- `RATE_LIMITED`：列表或查询超过限速，应遵守返回的
  `retry_after_seconds`。

## 资源生命周期与删除

```text
create_db_instance(client_token, db_type, name?)
        |
        v
     CREATING ---- 供应失败 ----> FAILED
        |
        v
       READY
        |
delete_db_instance(db_instance_id)
        |
        v
     DELETING ---- 清理失败 ----> DELETE_FAILED
        |
        v
      DELETED
```

创建操作持久保留容量并返回 `CREATING`；后台 Worker 完成租户、资源配置、
账号、数据库、授权和连接验证。只有 `READY` 状态会暴露供应资源的连接凭证。

删除请求会立即停止暴露凭证。清理过程会锁定账号、终止活动连接、删除数据库
对象和租户资源配置、验证没有残留、销毁加密资源凭证，最后释放容量。验证完成
前不会释放容量。`delete_db_instance` 在可删除状态下具有幂等性。

资源行及其 `client_token` 在 `DELETED` 后仍永久保留，作为幂等和审计历史。
删除始终由调用方显式发起；不存在按资源年龄自动将其转为 `DELETING` 的后台
扫描。

## 运维与恢复

健康和调度 Worker 从元数据库读取后端配置。一个 Worker 不再持有处理权后，
其他进程可以继续任务。供应和清理失败使用有上限的重试，并保留上一个已完成
步骤。

后端发生故障时：

1. 将其设为 `draining`，停止新资源放置，已有任务继续。
2. 解决网络、凭证、健康或容量问题。
3. 验证后重新激活；也可以设为 `disabled`，同时保留清理和恢复路径。
4. 对 `FAILED` 或 `DELETE_FAILED` 查看脱敏服务日志和资源步骤，绝不把含
   秘密的响应复制到工单。

仍有资源需要清理时，不要吊销后端的 `provisioning_admin` 凭证。Agent 仍
拥有该后端上的未删除资源时，不能移除聚合实例访问；应先取消
**Create managed databases**，删除已有资源后再移除访问记录。

## 功能验收

运行生命周期、授权和 UI 管理契约的聚焦测试：

```bash
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: uv run --extra dev pytest \
  tests/test_db_instance_e2e.py \
  tests/test_dynamic_tool_catalog.py \
  tests/test_agent_binding_admin_api.py \
  tests/test_provisioning_backend_admin_api.py -q
```

运行完整后端测试和前端检查：

```bash
PAS_DATABASE_URL=sqlite+aiosqlite:///:memory: uv run --extra dev pytest -q

cd web
npm test -- --run
npm run lint
npm run build
```

本地测试使用模拟数据库操作，不能证明 VPC 连通性或真实 PolarDB 多租户 DDL。
集成和性能验收应使用受控 VPC 环境。
