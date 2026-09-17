# REST API 参考

[English](../../en/reference/rest-api.md)

Web 控制台使用 `/api` 下经过认证的 REST API。MCP 客户端使用 Streamable
HTTP 端点 `/mcp`。`/mcp/rest` 前缀同时包含旧的人类用户 SQL 路由和 Agent
数据库生命周期路由；每个路由族强制使用各自的 Principal 类型。

## 认证与安全

人类管理请求使用管理员 Session Cookie 和 `X-PAS-CSRF: 1`，或受支持的
管理员 Bearer Token。Agent Token 不能调用管理员 API。setup 期间，
`POST /api/config` 只接受有效的 `Authorization: Bootstrap ...` claim。

响应使用稳定错误码和脱敏消息，不要依赖原始异常文本。配置激活和停用等变更
动作要求幂等和 revision 控制。

## 主要资源

管理路由包括 `/api/users`、`/api/departments`、`/api/instances`、
`/api/agents`、`/api/credentials`、`/api/provisioning-backends`、
`/api/audit-logs`。嵌套 User 和 Agent 路由
管理实例/供应绑定及自有资源。
Dedicated 管理还增加 `/api/dedicated-pools`、`/api/permission-templates`、
`/api/permission-template-revisions/{id}/sync`、`/api/permission-sync-jobs`
和 `/api/db-instance-resources`。

`/api/polarrag` 提供仅管理员可用的 PolarRAG 实例检查、可信 Space 启用与
同步，以及逐用户企业主体映射。密钥字段只写不读。详见
[PolarRAG MCP 与企业身份](../knowledge/polarrag-mcp.md)。

[企业身份源管理员 API](enterprise-identity-sources-api.md)单独列出飞书与
SharePoint 身份源、Space 绑定、用户身份映射和飞书 ACL 成员快照的管理接口、参数与作用。

Agent 范围的 PolarRAG 访问通过
`/api/agents/{agent_id}/polarrag-bindings` 和
`/api/agents/{agent_id}/user-assignments` 管理。已认证用户通过
`/api/me/agent-connections` 查看自己的分配关系，并用嵌套的 `issue`、
`reveal`、`regenerate` 和 `revoke` 操作管理自己的 Token。敏感响应带有
`Cache-Control: no-store`。`reveal` 使用当前已认证的 builtin 或 SSO Session，
不会二次要求密码，并继续执行本人归属校验、审计和限流；管理员响应永远不包含
用户 Token 明文。

### 用户 Workspace

`GET /api/me/workspace` 返回当前已认证 User 的 Workspace、默认 Agent 和可用的
活动 Agent。`status` 取值为：

- `ready`：已选择的默认 Agent 仍然可用；
- `selection_required`：存在多个可用 Agent，但尚未选择；
- `no_agent_access`：User 没有已授权的活动 Agent；
- `default_agent_unavailable`：已保存 Agent 被停用或不再授权。

恰好只有一个可用 Agent 时，GET 操作会自动选择。
`PUT /api/me/workspace/default-agent` 接受 `{"agent_id": "..."}`，且只能选择
当前用户可用的 Agent。默认 Agent 失效后，PAS 会保留原 ID 并拒绝 MCP Tool
执行，不会静默选择替代项。

### SSO 配置测试

保存并验证 `user_sso` 草稿后，`POST /api/config/user-sso/tests` 返回
`id`、`status`、`authorize_url` 和 `expires_at`。当前管理员在浏览器打开
`authorize_url`，随后轮询 `GET /api/config/user-sso/tests/{id}`，状态可能为
`pending`、`exchanging`、`passed` 或 `failed`。

通过常规 `POST /api/config` 激活 `user_sso` 时增加 `sso_test_id`：

```json
{
  "protocol_version": 1,
  "action": "activate",
  "module": "user_sso",
  "expected_revision": 4,
  "validation_id": "VALIDATION_ID",
  "sso_test_id": "SSO_TEST_ID",
  "idempotency_key": "REQUEST_ID"
}
```

测试证明仅归发起管理员所有，具有有效期，配置变化后会失效，并在激活事务中
原子消费。

管理员通过
`GET /api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources`
列出某个绑定下符合条件的已同步 ACTIVE PUBLIC 资源，并在同一路由使用 `PUT` 更新
范围。请求字段 `public_knowledge_resource_ids` 为 `null` 时表示实例的全部 PUBLIC
资源，为数组时表示指定范围，为空数组时表示不允许任何 PUBLIC 资源。PERSONAL ID
会被拒绝。更新会以 `agent_polarrag_binding.public_scope.update` 写入审计，并在下一次
MCP Tool 调用生效，无需重新签发 Token。

Agent 手工知识绑定通过
`POST /api/admin/agents/{agent_id}/knowledge-bindings:batch` 原子更新，兼容模式通过
`PUT /api/admin/agents/{agent_id}/knowledge-scope-mode` 修改。身份源自动化使用幂等的
`PUT/DELETE /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}`；
本地外部主体快照使用
`PUT /api/identity-sources/{source_id}/user-principal-memberships`。

管理员通过
`PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode`
将知识资源设为 `NATIVE` 或 `EXTERNAL_SYNC`。PAS 用户及 MCP 对 `EXTERNAL_SYNC`
资源发起的文档写操作会被拒绝。

实例注册提供创建前和已有实例的连接测试端点。凭证创建/更新有独立测试动作。
连接测试从后端 Pod 执行。

原资源池 Router 和人类用户配额 Router 已整体退役。人类用户请求不再自动购买
物理集群；未分配实例的 User 会收到 `NO_INSTANCE_ASSIGNED`，并应联系管理员。

## Agent 数据库生命周期

Agent Bearer Token 调用 `POST /mcp/rest/db-instances`、
`GET /mcp/rest/db-instances/{resource_id}` 和
`DELETE /mcp/rest/db-instances/{resource_id}`。人类 JWT 和 Cookie 会被拒绝。
响应使用 `Cache-Control: no-store`；创建和可重试状态会按需提供 `Location` 或
`Retry-After`。

示例、状态语义、幂等、错误和凭证处理参见
[Agent REST 数据库供应](../database-instances/agent-rest-provisioning.md)。

## 引导式配置

`POST /api/config` 使用一个带版本的命令 Envelope：

```json
{
  "protocol_version": 1,
  "action": "describe",
  "module": "runtime_policy"
}
```

动作包括 `describe`、`plan`、`save_draft`、`validate`、`activate`、
`skip`、`reset`、`disable` 和 `export`。副作用必须提供命令契约要求的字段，
包括适用时的 `expected_revision`、验证凭据或幂等键。激活 `user_sso` 还必须
提供上文所述的成功浏览器测试证明。

## 内部生命周期 API

运维人员显式启用独立管理监听器后，它会提供
`GET /api/internal/v1/status`、`POST /api/internal/v1/config`，以及下文的托管账号
路由。该监听器必须由运维人员显式启用，并且与业务监听器隔离：这些路由
不属于客户 REST API，也不属于业务监听器生成的 OpenAPI。不要通过业务
Service、Ingress 或公共 API 网关暴露它们。

一期变更白名单每次只接受一个 `core_admin` 参数：托管初始化时使用
`username`，只有从 `RESET_REQUIRED` 一次性迁移到 `ACTIVE` 时才能使用
`password`。托管 `save_draft`、`validate`、`activate` 和
`set_initial_password` 命令都要求幂等键。密码激活后使用新操作会返回
`ADMIN_PASSWORD_ALREADY_INITIALIZED`；之后的普通密码修改必须使用已认证的
PAS Session 和当前密码。

### 托管管理员账号

该监听器只管理一个固定的内置账号 `admin`。使用
`GET /api/internal/v1/accounts`，并提供已绑定的 `instance_id` 和 `generation`；可选的
`account_name` 只能是 `admin`。响应通过 `account_name`、`account_status` 和
`password_status` 标明账号及其状态，不会包含密码。

使用 `POST /api/internal/v1/accounts/admin/password` 更改该账号的密码。其带版本
Envelope 包含 `target`（`instance_id` 和 `generation`）、`control_user` actor、
`operation`、`new_password` 和 `idempotency_key`。`protocol_version` 是必填字段，且
必须为 `1`；省略时 PAS 不会使用默认版本。操作决定对旧密码的要求：

| 操作 | 允许的密码状态 | 旧密码规则 |
| --- | --- | --- |
| `MODIFY` | `ACTIVE` | 必须提供 `old_password`，并且它必须是当前密码。 |
| `RESET` | `RESET_REQUIRED` 或 `ACTIVE` | 不接受 `old_password`。 |

现有的 `set_initial_password` 配置命令仍是从 `RESET_REQUIRED` 到 `ACTIVE` 的独立、
仅限首次的初始化路径。激活后重复调用会返回
`ADMIN_PASSWORD_ALREADY_INITIALIZED`。

每次成功的内置密码初始化、修改或重置都会递增凭证 epoch，吊销 REST 和 MCP OAuth
refresh token，使尚未使用的内置 authorization code 失效，并拒绝已有的密码认证访问
会话。任何端点都不会返回密码，包括成功的账号响应、幂等重放或错误响应。使用同一幂等键
但请求不同时，PAS 继续返回 `IDEMPOTENCY_CONFLICT`。

PAS 根据 `control_user` 模型校验请求提交的 actor，并把该 actor 与请求 Envelope 的其余
内容一起纳入用于幂等比较的用途隔离 HMAC。管理监听器认证以及已配置的实例 ID 和
generation 共同建立可信边界；PAS 不会重建请求提交的 actor。响应、回执和日志都不会
包含传入密码。客户操作应使用客户 OpenAPI 和 UI，客户客户端绝不能直接调用该内部监听器。

## OpenAPI 发现

应用 Schema `/openapi.json` 排除 Agent 生命周期路由。Agent 使用规范的独立
Schema `/mcp/rest/openapi.json`；人类可以查看 `/mcp/rest/docs`。CI 同时验证
两个 Surface，避免一个 Principal 发现或误用另一个契约。应把实际部署版本的
Schema 作为权威，并使用不可变发布 Tag 对应的文档示例。

## 账号与资源控制台 API

| 方法 | 路径 | 访问要求 |
| --- | --- | --- |
| GET | `/api/access/accounts?kind=personal` | 管理员；`kind=service` 选择服务账号 |
| GET | `/api/access/resources?kind=database` | 管理员；`kind=knowledge` 遵守知识库可用状态 |
| GET | `/api/access/grants` | 管理员；按 `resource_id` 或成对的 `account_kind`、`account_id` 筛选 |
| PUT | `/api/access/accounts/{kind}/{account_id}/resources/{instance_id}` | 管理员；SQL 预设，`credential_id` 与 `permission` |
| POST | `/api/access/accounts/{kind}/{account_id}/resources/{instance_id}/disable` | 管理员；保留禁用的直接绑定 |
| GET | `/api/me/resources?personal=true` | 当前用户资源范围，无需选择 Agent |
| GET / POST | `/api/me/personal-tokens` | 仅当前用户；POST 接受 `expires_in_days`（1–365，默认 90） |
| DELETE | `/api/me/personal-tokens/{token_id}` | 仅当前 Token 所有者 |

账号、资源与授权列表采用 `offset`、`limit` 分页；账号与资源列表支持 `search`。授权 `kind` 为 `personal` 或 `service`。个人 Token 明文只在 POST 时返回一次，GET 仅返回状态；端点复用现有身份与绑定存储。
