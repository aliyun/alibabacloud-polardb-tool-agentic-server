# 企业身份源管理员 API

[English](../../en/reference/enterprise-identity-sources-api.md)

本页描述企业身份源的管理员自动化接口。控制台操作优先使用
[企业身份源](../administration/enterprise-identity-sources.md)指南；脚本、运维平台或集成系统才应调用以下 API。

所有接口均以 `/api` 为前缀，要求管理员 Session Cookie 与 `X-PAS-CSRF: 1`，或受支持的管理员 Bearer Token。密钥字段仅可写入，所有响应均不会返回明文密钥。

## 身份源

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 创建身份源 | `POST /api/identity-sources` | `name`、`provider`、可选 `stale_after_seconds`；飞书使用 `app_id`、`app_secret`；SharePoint 使用 `tenant_id`、`cloud`、`client_id`、`client_secret` | 创建待验证或待绑定身份源。`provider` 只能是 `feishu` 或 `sharepoint`。 |
| 发起飞书租户验证 | `POST /api/identity-sources/{source_id}/feishu-verification` | 路径参数 `source_id` | 返回一次性的 `authorization_url`；管理员在浏览器中打开该地址完成飞书授权。仅待验证飞书来源可调用。 |
| 列出身份源 | `GET /api/identity-sources` | `offset`（默认 `0`）、`limit`（默认 `20`、最大 `100`）、可选 `search` | 返回 `{items, total, offset, limit}`。先按来源名称搜索，再做分页；每项包含来源状态、已绑定 Space、最近同步时间、脱敏 `last_error`，以及飞书成员关系部分成功时独立的结构化 `sync_warning`。 |
| 立即同步 | `POST /api/identity-sources/{source_id}/sync` | 路径参数 `source_id` | 启动或加入一个已就绪飞书或 SharePoint 来源的串行刷新。请求预算耗尽或已有任务运行时返回 `202`，并显示 `last_error=SYNC_IN_PROGRESS`；后台任务继续执行。 |
| 更新身份源 | `PUT /api/identity-sources/{source_id}` | 路径参数 `source_id`；`name` 与当前提供方的完整凭证。飞书使用 `app_id`、`app_secret`；SharePoint 使用 `cloud`、`client_id`、`client_secret` | 更新凭证并清除最近同步状态。飞书必须重新验证租户；SharePoint 保留原租户 ID，重新绑定后同步。 |
| 删除身份源 | `DELETE /api/identity-sources/{source_id}` | 路径参数 `source_id` | 删除来源、目录快照、Space 绑定和由该来源派生的授权；既有 PAS 用户不会删除。 |
| 查看同步目录 | `GET /api/identity-sources/{source_id}/directory` | 路径参数 `source_id`；`entry_type=users|groups|all`、`offset`、`limit`（默认 `20`、最大 `100`）、可选 `search` | 分页返回同步用户、组或两者，用于运维核查与用户身份映射选择。先按稳定外部 ID 和展示字段搜索，再做分页。 |

`source_id` 是创建或列表响应中的身份源 UUID。同步失败时 API 仅返回脱敏后的错误类型；部分成员关系 warning 只包含 `code`、`skipped_count` 和 provider code 计数，不包含上游响应正文或凭证。应结合受保护的 PAS 日志排查，不能记录或传输密钥。

目录响应包含 `users`、`groups` 和 `total`。当 `entry_type=users` 或 `groups`
时，`total` 是该类型过滤后的总数；当 `entry_type=all` 时，同一个 `offset` 和
`limit` 分别作用于两个数组，`total` 为 `null`。需要完整翻页和精确总数时，应按
类型分别请求。控制台只使用这些后端分页，不会在一次响应中加载完整租户目录。

## Space 绑定

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 列出可绑定 Space | `GET /api/identity-sources/spaces` | `offset`（默认 `0`）、`limit`（默认 `20`、最大 `100`）、可选 `search` | 返回已启用 PolarRAG Space 的 `{items, total, offset, limit}`；先按 Space ID、名称或身份域搜索，再做分页。 |
| 绑定 Space | `POST /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | 路径参数 `source_id`、`knowledge_space_id` | 让该来源的已同步主体在目标 Space 中参与 ACL context 构造；重复调用保持幂等。 |
| 解绑 Space | `DELETE /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | 路径参数 `source_id`、`knowledge_space_id` | 立即停止将该来源主体用于目标 Space；不删除身份源或已同步用户。 |

绑定只使主体成为候选 ACL 主体，不授予任何知识库或文档访问权限；最终访问仍由 PolarRAG ACL 裁决。

## Agent 企业访问编排

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 预览 Agent 企业访问 | `POST /api/agents/{agent_id}/enterprise-access/preview` | `identity_source_id`、显式 `all_synced_users`、`directory_group_ids`、`pas_user_ids`、可选 `polarrag_instance_ids`、`knowledge_space_ids` | 只读执行权威校验，返回规范化 `selection`、Agent 局部 `creates`、全局 Source-Space `global_changes`、既有 `reuses` 和 `preview_hash`。 |
| 应用已确认访问 | `POST /api/agents/{agent_id}/enterprise-access/apply` | 完整返回的 selection 加 `preview_hash` | 原子、幂等地创建缺失的 Agent-实例绑定、Source-Space 绑定和 Agent 主体授权，并记录必需审计。任何校验、写入、提交或审计失败都会回滚整个操作。 |

请求只包含 PAS 内部的身份源、目录行、用户和 Space ID，不能包含 `provider`、
`principal_id`、`identity_domain`、凭证或 `acl_context`；PAS 从当前服务端记录推导这些
可信值。`all_synced_users` 缺省为 `false`，但客户端应始终显式发送当前选择。请求至少
包含一个主体和一个已启用 Space。客户端应发送非空 `polarrag_instance_ids` 列表；为了
兼容省略该字段的 v0.0.9 请求，PAS 会从所选的已启用 Space 推导排序后的实例 ID，校验
每个推导实例均为活动状态，并在规范化 `selection` 中返回这些 ID。显式实例 ID 列表仍
不得为空，并且每个所选实例至少贡献一个所选的已启用 Space。
每个请求最多接受 500 个 `directory_group_ids`、500 个 `pas_user_ids`，以及 200 个
`polarrag_instance_ids` 和 `knowledge_space_ids`。

应用时会重新校验 Source 新鲜度、目录状态、实例与 Space 状态、Agent-实例绑定和预览关系。
若状态变化，接口不写入任何内容，返回 `409`、
`detail.code=ENTERPRISE_ACCESS_PREVIEW_STALE` 以及刷新后的预览。客户端必须展示新预览并
再次确认，不能自动应用。新增 Source-Space 绑定属于全局共享状态；后续删除对应 Agent
用户、组或全部同步用户授权时，不会删除这些全局绑定、Agent-实例绑定或共享 PUBLIC 范围。

## Agent 知识库范围

| 操作 | 方法和路径 | 作用 |
| --- | --- | --- |
| 查看绑定关系 | `GET /api/admin/agents/{agent_id}/knowledge-bindings` | 按主体分页返回绑定来源、主体标识、解析后的 Space/KB 标识和 Agent 范围模式；支持 `search`、`origin`、`subject_type`。 |
| 查看资源候选项 | `GET /api/admin/agents/{agent_id}/knowledge-resource-options` | 分页搜索 Agent 全局 PolarRAG 上限内的有效知识资源。 |
| 批量维护手工绑定 | `POST /api/admin/agents/{agent_id}/knowledge-bindings:batch` | 在一个事务中按顺序执行最多 100 组 `BIND` 或 `UNBIND`；`activate_scoped_mode=true` 时切换为 `SCOPED`。 |
| 修改模式 | `PUT /api/admin/agents/{agent_id}/knowledge-scope-mode` | 将 `mode` 设置为 `LEGACY_ALL` 或 `SCOPED`。 |
| 替换外部绑定 | `PUT /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` | 幂等地完整替换一个来源管理的绑定快照。 |
| 删除外部绑定 | `DELETE /api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` | 删除一个来源管理的绑定快照。 |

每组操作或外部快照都包含 `subjects` 和 `targets`。主体只能是
`{"type":"USER","user_id":"..."}`、
`{"type":"DEPARTMENT","department_id":"..."}` 或
`{"type":"GROUP","identity_source_id":"...","group_id":"..."}` 之一；
目标只能填写 `knowledge_resource_id`，或同时填写 `space_id`、`kb_id`。PAS
在校验前对主体和目标去重。单批最多接受 5000 个不同主体；单组操作或外部快照
最多接受 `max_exhaustive_knowledge_resources` 个不同目标。

`external_scope_id` 是来源系统的稳定范围标识，不是 PAS scope ID，也不是
PolarRAG KB ID。飞书 Wiki 同步时该值填写飞书 Wiki `space_id`。幂等键为
`(agent_id, identity_source_id, external_scope_id)`。超过配置上限时应提高配置，
不能把同一个外部 scope 拆分传入；PAS 不会自动分片。

命中的用户直属范围和组范围按并集生效，再与 Agent 全局 PolarRAG 范围及用户可见资源
取交集；PolarRAG 文档 ACL 仍是最终授权门禁。并集超过
`max_exhaustive_knowledge_resources` 时，穷举搜索会失败关闭，不会静默漏搜 KB。

## PAS 用户与企业身份映射

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 查看用户映射 | `GET /api/identity-sources/users/{user_id}/identities` | 路径参数 `user_id`；`offset`（默认 `0`）、`limit`（默认 `20`、最大 `100`）、可选 `search` | 返回 PAS 用户已关联企业身份的 `{items, total, offset, limit}`，包含用户、部门、用户组和原生 PolarRAG 主体；先按稳定外部 subject 搜索，再做分页。 |
| 绑定企业身份 | `POST /api/identity-sources/users/{user_id}/identities` | 路径参数 `user_id`；请求体 `identity_source_id`、`external_user_id` | 将已同步企业用户映射到已有 PAS 用户；若该企业身份当前属于其自动创建的 PAS 用户，则安全转移映射。 |
| 修改映射 | `PUT /api/identity-sources/users/{user_id}/identities/{identity_id}` | 路径参数 `user_id`、`identity_id`；请求体 `identity_source_id`、`external_user_id` | 用另一已同步企业用户替换手工维护的映射。自动同步用户自身的主身份不可编辑。 |
| 删除映射 | `DELETE /api/identity-sources/users/{user_id}/identities/{identity_id}` | 路径参数 `user_id`、`identity_id` | 移除手工维护的映射并恢复企业身份的默认同步用户。自动同步用户自身的主身份不可删除。 |

`external_user_id` 必须从身份源目录接口返回的稳定外部用户 ID 中选择，不能使用邮箱或展示名猜测。

## 外部主体成员关系与旧飞书快照

`PUT /api/identity-sources/{source_id}/user-principal-memberships` 每次最多接受
100 个用户、50,000 条成员关系。每个用户提交完整的 `principals` 列表，字段包含
`principal_type`、`principal_id` 和可选 `expires_at`；空列表
清空该用户，未出现的用户保持不变。PAS 使用未过期记录展开 ACL context 和匹配 Agent
组分配。用户无需已存在于目录快照中。

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 配置成员快照 | `PUT /api/identity-sources/{source_id}/acl-membership-snapshot` | 路径参数 `source_id`；请求体 `host`、可选 `port`、可选 `database`、`username`、`password` | 为已验证飞书来源保存 ACL 成员快照连接。更新后来源回到待绑定状态，需再次同步。 |

该接口仅用于确有 ETL ACL 成员快照的飞书部署；正常飞书或 SharePoint 目录同步不需要调用。密码为只写字段，必须由受保护的自动化环境提供。
本地批量接口与旧数据库快照互斥：任一后端已配置时，配置另一后端会返回 `409` 和
`ACL_MEMBERSHIP_BACKEND_CONFLICT`。
