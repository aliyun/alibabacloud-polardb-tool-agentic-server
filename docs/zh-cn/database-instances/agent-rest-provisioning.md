# Agent REST 数据库供应

[English](../../en/database-instances/agent-rest-provisioning.md)

本文是 Agent 通过 REST 创建、查看和删除 PAS 托管数据库的人类可读契约。
同一 PAS 部署在 `/mcp/rest/openapi.json` 提供机器可读契约。

## 认证与隔离

Agent Token 只能放在 HTTP Authorization Header 中发送：

```http
Authorization: Bearer <agent-token>
Content-Type: application/json
```

Token 代表唯一 Agent。Agent 只能查看自己的资源；不存在的资源与其他 Agent
的资源都返回 `RESOURCE_NOT_FOUND`。人类 JWT、管理员 Cookie 和 bootstrap token
不能调用这些路由。反过来，Agent Token 不能调用 `/mcp/rest` 下的人类用户路由
或 `/api` 下的管理员路由。

所有生命周期响应都使用 `Cache-Control: no-store`。绝不能把 Token、数据库密码
或完整响应体放入 URL、日志、Trace 属性、Prompt、分析事件或持久缓存。

## 发现契约

- Agent 详情页展示生命周期 URL，并提供当前服务 API 文档和本指南的入口；REST
  复用页面上为 MCP 展示的同一个 Agent Token。
- `GET /mcp/rest/openapi.json` 只返回 Agent 生命周期 Schema。
- `GET /mcp/rest/docs` 为人类读者渲染这份独立 Schema。
- 应用级 `/openapi.json` 有意排除 Agent 生命周期路由。

应以实际部署 PAS 版本提供的 Schema 为准。生命周期入口复用 MCP REST 端口，
但认证主体和入口不同；内部生命周期实现与 MCP Tool 共用。GitHub 指南是补充说明，
可能描述比当前 Agent 页面所连接 PAS 部署更新的已发布版本。

## 创建数据库

调用 `POST /mcp/rest/db-instances`，传入永久 `client_token`、引擎和显式供应模式：

```json
{
  "client_token": "job-018f7f2d",
  "name": "orders-sandbox",
  "db_type": "polardb_mysql",
  "provisioning_mode": "dedicated"
}
```

`provisioning_mode` 必填，可取 `multitenant` 或 `dedicated`。Dedicated 操作预算
只约束 Dedicated 供应，因为 Dedicated 的创建/删除循环可能购买物理集群。
Multitenant 操作由活跃资源配额约束，不会触发物理购买；这种差异是有意设计。

`client_token` 在 Agent 内唯一，并永久绑定到规范化请求。相同请求重放会返回
相同 `resource_id`，包括资源已经 `DELETED` 的情况。使用同一 Key 提交不同输入
会返回 `IDEMPOTENCY_CONFLICT`。

Dedicated 热分配返回 `201` 和 `READY`。冷分配返回 `202`、`CREATING`、
`Location`、`Retry-After` 和 `retry_after_seconds`。容量或操作预算拒绝返回 `429`，
但不会削弱删除或断开连接的安全性。

## 轮询和使用数据库

按照返回的间隔或更长间隔轮询
`GET /mcp/rest/db-instances/{resource_id}`。状态全集为 `CREATING`、`READY`、
`FAILED`、`DELETING`、`COOLING_DOWN`、`RESTORING`、`DELETED` 和
`DELETE_FAILED`。

只有 `READY` 包含 `connection`：

```json
{
  "resource_id": "00000000-0000-0000-0000-000000000000",
  "status": "READY",
  "provisioning_mode": "dedicated",
  "name": "orders-sandbox",
  "db_type": "polardb_mysql",
  "source": "provisioned",
  "connection": {
    "host": "example.mysql.polardb.rds.aliyuncs.com",
    "port": 3306,
    "database": "agentic_example",
    "username": "agentic_example",
    "password": "<database-password>"
  }
}
```

应把连接字段当作一个密钥包，只在工作负载需要期间保留。`FAILED` 是创建的终态，
不返回凭证，并且幂等重放时保持稳定。

## 删除与冷却

工作完成后调用 `DELETE /mcp/rest/db-instances/{resource_id}`。请求是幂等的：
永久删除的资源返回 `204`。

PAS 先撤销资源访问并断开现有 Session。对于 Dedicated 资源，只有在确认断开后
才开始计算 `cooldown_until`。有效 `delete_cooldown_duration_hours` 依次取最具体的
资源/成员覆盖、自动供给池覆盖和全局值；全局默认 24 小时，有意设置的最小值为 1 小时。

处于 `COOLING_DOWN` 时，成员不计入自动供给池 planning 容量，但仍计入计费相关的
`max_total_members`。Agent 不能恢复资源。管理员可从 `DELETING`、
`DELETE_FAILED` 或 `COOLING_DOWN` 恢复仍可逆的资源；PAS 只有在验证成功后才
重新启用原数据库和账号凭证。开始 sanitize 或物理销毁后不能恢复。

## 错误与重试行为

错误包含稳定 `code`、脱敏 `message` 和 `request_id`。可重试响应还包含
`Retry-After` 和 `retry_after_seconds`。至少处理以下错误码：

- `INVALID_ARGUMENT` 和 `UNSUPPORTED_PROVISIONING_MODE`：修正请求。
- `NO_ELIGIBLE_BACKEND`：请管理员为所需模式启用并绑定健康 Backend。
- `POOL_CAPACITY_LIMIT_REACHED`：等待容量或管理员变更，不能循环创建/删除。
- `RATE_LIMITED`：至少等待返回的间隔。
- `IDEMPOTENCY_CONFLICT`：使用原请求，或改用新的 `client_token`。
- `RESOURCE_STATE_CONFLICT`：刷新资源后再重试动作。
- `PROVISIONING_FAILED` 和 `DISCONNECT_FAILED`：保留 `request_id`，请运维人员
  查看脱敏服务端日志。
- `RESOURCE_NOT_FOUND` 和 `UNAUTHORIZED`：不能探测其他标识，也不能换用人类
  凭证重试。

瞬时故障使用带抖动的指数退避。即使创建/删除速率或购买预算已耗尽，也必须允许
删除请求，因为撤销访问的优先级高于配额执行。

## Agent 工作流示例

以下顺序对两种供应模式都安全：

```text
使用新的 client_token 发起 POST create
如果是 CREATING：按照 Retry-After 轮询 Location
如果是 READY：使用 connection，但不持久化 password
在 finally/cleanup 路径 DELETE 资源
仅在调用方需要观察断开或最终清理时继续轮询
```

MCP Agent 也可以使用 `create_db_instance`、`describe_db_instance` 和
`delete_db_instance`；REST 与 MCP 共用相同的资源、幂等、权限、冷却和清理实现。
