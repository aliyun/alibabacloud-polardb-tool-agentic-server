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
| 列出身份源 | `GET /api/identity-sources` | `offset`（默认 `0`）、`limit`（默认 `20`、最大 `100`）、可选 `search` | 返回 `{items, total, offset, limit}`。先按来源名称搜索，再做分页；每项包含来源状态、已绑定 Space、最近同步时间、脱敏 `last_error`、飞书 `event_subscription`，以及飞书成员关系部分成功时独立的结构化 `sync_warning`。 |
| 立即同步 | `POST /api/identity-sources/{source_id}/sync` | 路径参数 `source_id` | 启动或加入已就绪来源的串行全量刷新。飞书立即返回 `202` 和 `full_sync_in_progress`；已接收的增量事件处理完成后才开始全量对账。SharePoint 保留同步响应；请求预算耗尽时返回 `202` 和 `last_error=SYNC_IN_PROGRESS`。 |
| 更新身份源 | `PUT /api/identity-sources/{source_id}` | 路径参数 `source_id`；`name` 与当前提供方的完整凭证。飞书使用 `app_id`、`app_secret`；SharePoint 使用 `cloud`、`client_id`、`client_secret` | 更新凭证并清除最近同步状态。飞书必须重新验证租户；SharePoint 保留原租户 ID，重新绑定后同步。 |
| 删除身份源 | `DELETE /api/identity-sources/{source_id}` | 路径参数 `source_id` | 删除来源、目录快照、Space 绑定和由该来源派生的授权；既有 PAS 用户不会删除。 |
| 查看同步目录 | `GET /api/identity-sources/{source_id}/directory` | 路径参数 `source_id`；`entry_type=users|groups|all`、`offset`、`limit`（默认 `20`、最大 `100`）、可选 `search` | 分页返回同步用户、组或两者，用于运维核查与用户身份映射选择。先按稳定外部 ID 和展示字段搜索，再做分页。 |

`source_id` 是创建或列表响应中的身份源 UUID。同步失败时 API 仅返回脱敏后的错误类型；部分成员关系 warning 只包含 `code`、`skipped_count` 和 provider code 计数，不包含上游响应正文或凭证。应结合受保护的 PAS 日志排查，不能记录或传输密钥。

目录响应包含 `users`、`groups` 和 `total`。当 `entry_type=users` 或 `groups`
时，`total` 是该类型过滤后的总数；当 `entry_type=all` 时，同一个 `offset` 和
`limit` 分别作用于两个数组，`total` 为 `null`。需要完整翻页和精确总数时，应按
类型分别请求。控制台只使用这些后端分页，不会在一次响应中加载完整租户目录。

身份源响应包含 `snapshot_usable`：状态为 active，或者状态为 stale 且最近成功快照
仍在 `stale_after_seconds` 有效期内时为 `true`。没有成功快照的 stale 来源不可用。
该字段不替代外部 Token 校验，也不替代知识库和文档 ACL 校验。

飞书来源的 `event_subscription` 包含 `status` 和 `fallback_after_seconds`
（可配，默认 `86400`）。`status` 取值为 `connecting`、
`connected_unconfirmed`、`active` 或 `error`。SharePoint 返回
`event_subscription: null`。该对象不会包含连接 URL、凭据、Verification Token、
Encrypt Key 或原始事件正文。来源级 `full_sync_in_progress` 和 `group_sync_in_progress` 分别表示全量与用户组专项对账；
`last_group_synced_at` 和脱敏 `last_group_error` 展示组对账状态。飞书初次全量完成后不再固定每日全量对账：
仅当订阅连接持续不可用达到配置阈值，且距上次全量至少经过同样时长，才触发全量兜底；全量前先建联，三次失败也继续全量并保留 `error`。连接已建立但无事件属于正常状态。修复飞书配置后可调用 `POST /api/identity-sources/{source_id}/sync` 立即先建联再全量对账。用户组按独立周期
专项对账（默认一小时）。成功处理事件会清除不可用时段；管理员也可随时主动发起全量同步。

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
| 查看资源候选项 | `GET /api/admin/agents/{agent_id}/knowledge-resource-options` | 分页搜索已绑定实例及符合父级约束 Space 下的有效 KB 候选项。可选 `binding_status=bound\|unbound` 在分页和统计前按 Agent 可见范围筛选。 |
| 批量维护手工绑定 | `POST /api/admin/agents/{agent_id}/knowledge-bindings:batch` | 在一个事务中按顺序执行最多 100 组 `BIND` 或 `UNBIND`；`activate_scoped_mode=true` 时切换为 `SCOPED`。 |
| 读取外部自动绑定 | `GET /api/admin/agents/{agent_id}/external-knowledge-auto-bind` | 仅管理员。返回 `agent_id`、`enabled`、`knowledge_scope_mode`，不修改权限。 |
| 设置外部自动绑定 | `PUT /api/admin/agents/{agent_id}/external-knowledge-auto-bind` | 仅管理员。请求体为 `{"enabled": true}` 或 `false`。开启会为该 Agent 重新投影已有快照；关闭移除同步范围及用户绑定，保留手工绑定，不改变访问模式。 |
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

Agent 的 `GET /api/agents/{agent_id}/user-options` 支持
`binding_status=bound|unbound`（默认 `unbound`），统一按 PAS 用户分页。
先将直接绑定和从已绑定企业身份源继承的访问归类，再计算总数和分页。
`GET /api/agents/{agent_id}/user-assignments` 保留为直接绑定及 Token 管理接口。
返回的用户条目包含 `identity_source_names` 数组。空数组表示没有关联的企业身份源，
控制台显示 `PAS`；多来源用户仍只有一条记录，不影响 `total`、`offset` 和 `limit`。
绑定操作继续使用 PAS 用户 ID。

`GET /api/agents/{agent_id}/group-options` 支持相同的 `binding_status` 筛选。
已绑定身份源覆盖的目录组归入 `bound`，不再出现在 `unbound` 中。
两个选项接口返回直接绑定的 `assignment_id` 和身份源继承的
`inherited_source_names`（组列表可省略默认空字段）。同时具有直接和继承绑定时
只展示一条记录。继承要求身份源快照仍有效；映射用户还必须有活跃目录条目或
未过期的本地主体快照。无关的 PAS 用户和本地组不继承该身份源绑定。

控制台将继承项标为已加入并说明来源；身份源整体授权仍在时不允许单独解绑子项。
如需单独管理，请先在当前 Agent 中解绑父级企业身份源；已有直接绑定保留，
之后可单独解除。此操作不改变全局身份源与 Space 关系及运行时授权规则。

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
| 配置成员快照 | `PUT /api/identity-sources/{source_id}/acl-membership-snapshot` | 路径参数 `source_id`；请求体 `host`、可选 `port`、可选 `database`、可选 `dialect`（`mysql` 或 `postgresql`）、`username`、编辑时可选的 `password` | 为已验证飞书来源保存只读 ETL 元数据连接。企业身份源定时任务会同时拉取 ACL 主体成员关系和外部 KB 权限快照。保存保留身份源状态，仅将独立的连接验证结果标记为未验证。 |

该接口仅用于确有 ETL ACL 成员快照的飞书部署；正常飞书或 SharePoint 目录同步不需要调用。密码为只写字段，必须由受保护的自动化环境提供。
本地批量接口与旧数据库快照互斥：任一后端已配置时，配置另一后端会返回 `409` 和
`ACL_MEMBERSHIP_BACKEND_CONFLICT`。

MySQL 默认端口为 `3306`，PostgreSQL 默认端口为 `5432`。来源响应通过
`acl_membership_snapshot_configured` 表示配置状态，`acl_membership_snapshot` 仅返回方言、地址、端口、
数据库和用户名，不返回密码。首次配置必须提供密码，编辑时省略密码则保留原加密密码。
参见[知识库权限数据源配置指南](../administration/enterprise-identity-sources.md#配置知识库权限数据源)。

## 企业用户选择与身份返回

接口仍要求管理员鉴权。企业身份对仅选择用户，不能作为认证凭据。原 PAS ID 和旧入口
继续有效；分页、排序、名称及 encoded `external_id` 的原含义保持。

知识绑定 `USER` 主体可传 `user_id`，或传完整企业身份对：

```json
{"type":"USER","identity_source_id":"source-id","external_user_id":"enterprise-subject"}
```

Agent 用户分配使用相同选择，省略 `type`。半套身份或混用两套选择返回 422；未知源或当前
映射返回 404，查询不创建、同步或分配用户。其他主体类型保持原字段组合。批次在原事务前
按 PAS 账户去重，旧/新表示同一账户只产生一个绑定。外部 scope 路径的源仅表示 scope 来源，
每个企业 USER 仍须明确自己的身份源。

企业访问 preview/apply 在已有 `identity_source_id` 下新增 `external_user_ids`，最多 500 个，
每个 ID 为 1–255 字符。`pas_user_ids` 默认仍为 `[]`，显式空数组与企业 ID 同传有效；两数组
都非空返回 422。进入原服务前解析当前映射，原 preview hash 算法不变。换绑可能使预览失效，
出现 stale 409 后须重新预览再 apply。返回 `selection` 保留 `pas_user_ids`，新增选定源下的
`external_user_ids`。

用户分配列表、用户选项、知识绑定 USER 主体和知识主体选项新增 `enterprise_identities`，
每项仅含 `identity_source_id` 与 `external_user_id`。完整返回所有当前身份并确定性排序；无映射
或用户已删除时返回 `[]`。显式映射归属优先于旧 canonical 回退；已经换绑给他人的身份不再
返回给旧账户。scope 保留 `user_ids`，新增 `enterprise_users`，每项含 `user_id` 和
`enterprise_identities`。Agent 创建/列表/详情/更新保留 `created_by`，新增
`created_by_enterprise_identities`；该字段仅输出，不能覆盖当前认证创建者。

身份映射增加四个兼容入口：

| Method | Path |
| --- | --- |
| GET | `/api/identity-sources/enterprise-users/identities` |
| POST | `/api/identity-sources/enterprise-users/identities` |
| PUT | `/api/identity-sources/enterprise-users/identities/{identity_id}` |
| DELETE | `/api/identity-sources/enterprise-users/identities/{identity_id}` |

query 必填 `identity_source_id` 和 `external_user_id`，仅定位目标 PAS 账户。GET 保留
`offset`、`limit`、`search`。POST/PUT body 的同名字段表示要绑定或替换的身份，其源可以与
query 目标源不同。query 值按标准编码，支持含 `/` 或 `:` 的 ID。原同步身份禁止编辑/解绑、
冲突检查、默认账户恢复和审计语义保持。原 `/users/{user_id}/identities` 四操作继续有效。

下列现有操作支持上述选择或新增身份返回：

| Method | Path |
| --- | --- |
| POST | `/api/agents/{agent_id}/enterprise-access/preview` |
| POST | `/api/agents/{agent_id}/enterprise-access/apply` |
| GET | `/api/admin/agents/{agent_id}/knowledge-bindings` |
| POST | `/api/admin/agents/{agent_id}/knowledge-binding-status` |
| POST | `/api/admin/agents/{agent_id}/knowledge-subject-options` |
| POST | `/api/admin/agents/{agent_id}/knowledge-bindings:batch` |
| PUT | `/api/admin/identity-sources/{source_id}/agents/{agent_id}/knowledge-bindings/{external_scope_id}` |
| GET | `/api/agents/{agent_id}/user-assignments` |
| POST | `/api/agents/{agent_id}/user-assignments` |
| GET | `/api/agents/{agent_id}/user-options` |
| POST | `/api/agents` |
| GET | `/api/agents` |
| GET | `/api/agents/{agent_id}` |
| PATCH | `/api/agents/{agent_id}` |
| GET | `/api/identity-sources/users/{user_id}/identities` |
| POST | `/api/identity-sources/users/{user_id}/identities` |
| PUT | `/api/identity-sources/users/{user_id}/identities/{identity_id}` |
| DELETE | `/api/identity-sources/users/{user_id}/identities/{identity_id}` |


## 独立 ETL 权限操作

| 操作 | 接口 | 行为 |
|---|---|---|
| 查询状态 | `GET /api/identity-sources/{source_id}/etl-permissions` | 仅管理员；返回连接验证、最近权限同步结果/时间/数量及脱敏错误码。 |
| 验证连接 | `POST /api/identity-sources/{source_id}/etl-permissions/verify` | 仅管理员，无请求体；返回 `202`。检查表的只读权限，不导入权限。 |
| 拉取 ETL 权限 | `POST /api/identity-sources/{source_id}/etl-permissions/sync` | 仅管理员，无请求体；返回 `202`。导入 KB 权限快照并重计算已开启自动绑定的 Agent，不同步目录。 |
| 重计算 Agent | `POST /api/admin/agents/{agent_id}/external-knowledge-bindings/reconcile` | 仅管理员，无请求体；返回 `200` 和快照/绑定数量。要求 SCOPED 且开启自动绑定，否则 `409`。仅使用 PAS 快照，不读取 ETL。 |

ETL 结果与身份源生命周期和目录错误分开记录。返回 `202` 后轮询状态接口：接受请求不代表同步完成。
`RUNNING`、`SUCCEEDED`、`PARTIAL`、`FAILED` 表示最近操作结果；`connection` 和 `sync` 分别保留
连接验证和权限导入结果。任务运行中重复触发 ETL 操作或编辑连接配置返回 `409`。
操作限时 120 秒；工作进程异常退出后租约在 300 秒后过期，可再次触发。响应不返回数据库原始异常或凭据。

## 钉钉

`POST /api/identity-sources` 支持 `provider="dingtalk"`，配置 `tenant_id`（corpId）、
`client_id`（AppKey）、`client_secret`（AppSecret）。初始状态为
`pending_tenant_verification`；`stale_after_seconds` 上限为 900。
`POST /api/identity-sources/{source_id}/dingtalk-verification` 要求管理员身份，返回
`authorization_url` 并设置绑定浏览器的 state cookie。回调为
`GET /auth/dingtalk/callback`，登录入口为 `GET /auth/dingtalk/login?source_id=...`。
两个入口均要求配置绝对 HTTP(S) `runtime_policy.external_base_url`。
URL 缺失或无效时，在创建授权 state 或 cookie 前返回 `409`：企业验证返回可操作的
JSON 配置错误，登录返回双语配置提示页。回调来源不会根据请求头推断。
激活有效 URL 后可重试。
state 只能消费一次，十分钟过期，应用凭据变更后失效。
来源响应增加 `permission_observed_at`、`permission_error` 和
`supported_principal_types=["user","department","domain"]`。
ETL 权限数据源配置与快照导入 API 同样支持钉钉。
知识资源发现接口使用 `access_origin=DINGTALK` 选择钉钉来源。
