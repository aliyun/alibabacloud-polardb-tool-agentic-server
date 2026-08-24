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
| 列出身份源 | `GET /api/identity-sources` | 无 | 返回来源状态、已绑定 Space、最近同步时间和脱敏错误类型。 |
| 立即同步 | `POST /api/identity-sources/{source_id}/sync` | 路径参数 `source_id` | 立即刷新已就绪的飞书或 SharePoint 目录快照。 |
| 更新身份源 | `PUT /api/identity-sources/{source_id}` | 路径参数 `source_id`；`name` 与当前提供方的完整凭证。飞书使用 `app_id`、`app_secret`；SharePoint 使用 `cloud`、`client_id`、`client_secret` | 更新凭证并清除最近同步状态。飞书必须重新验证租户；SharePoint 保留原租户 ID，重新绑定后同步。 |
| 删除身份源 | `DELETE /api/identity-sources/{source_id}` | 路径参数 `source_id` | 删除来源、目录快照、Space 绑定和由该来源派生的授权；既有 PAS 用户不会删除。 |
| 查看同步目录 | `GET /api/identity-sources/{source_id}/directory` | 路径参数 `source_id` | 返回最多 200 个同步用户和 200 个同步用户组，用于运维核查与用户身份映射选择。 |

`source_id` 是创建或列表响应中的身份源 UUID。同步失败时 API 仅返回脱敏后的错误类型；应结合受保护的 PAS 日志排查，不能记录或传输密钥。

## Space 绑定

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 列出可绑定 Space | `GET /api/identity-sources/spaces` | 无 | 返回已启用 PolarRAG Space 的 `knowledge_space_id`、名称和 `identity_domain`。 |
| 绑定 Space | `POST /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | 路径参数 `source_id`、`knowledge_space_id` | 让该来源的已同步主体在目标 Space 中参与 ACL context 构造；重复调用保持幂等。 |
| 解绑 Space | `DELETE /api/identity-sources/{source_id}/spaces/{knowledge_space_id}` | 路径参数 `source_id`、`knowledge_space_id` | 立即停止将该来源主体用于目标 Space；不删除身份源或已同步用户。 |

绑定只使主体成为候选 ACL 主体，不授予任何知识库或文档访问权限；最终访问仍由 PolarRAG ACL 裁决。

## Agent 企业访问编排

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 预览 Agent 企业访问 | `POST /api/agents/{agent_id}/enterprise-access/preview` | `identity_source_id`、显式 `all_synced_users`、`directory_group_ids`、`pas_user_ids`、`knowledge_space_ids` | 只读执行权威校验，返回规范化 `selection`、Agent 局部 `creates`、全局 Source-Space `global_changes`、既有 `reuses` 和 `preview_hash`。 |
| 应用已确认访问 | `POST /api/agents/{agent_id}/enterprise-access/apply` | 完整返回的 selection 加 `preview_hash` | 原子、幂等地创建缺失的 Source-Space 绑定和 Agent 主体授权，并记录必需审计。任何校验、写入、提交或审计失败都会回滚整个操作。 |

请求只包含 PAS 内部的身份源、目录行、用户和 Space ID，不能包含 `provider`、
`principal_id`、`identity_domain`、凭证或 `acl_context`；PAS 从当前服务端记录推导这些
可信值。`all_synced_users` 缺省为 `false`，但客户端应始终显式发送当前选择。请求至少
包含一个主体和一个 Space。
每个请求最多接受 500 个 `directory_group_ids`、500 个 `pas_user_ids` 和 200 个
`knowledge_space_ids`。

应用时会重新校验 Source 新鲜度、目录状态、Space 状态、Agent-实例绑定和预览关系。
若状态变化，接口不写入任何内容，返回 `409`、
`detail.code=ENTERPRISE_ACCESS_PREVIEW_STALE` 以及刷新后的预览。客户端必须展示新预览并
再次确认，不能自动应用。新增 Source-Space 绑定属于全局共享状态；后续删除对应 Agent
用户、组或全部同步用户授权时，不会删除这些全局绑定、Agent-实例绑定或共享 PUBLIC 范围。

## PAS 用户与企业身份映射

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 查看用户映射 | `GET /api/identity-sources/users/{user_id}/identities` | 路径参数 `user_id` | 返回 PAS 用户已关联的企业身份及其用户、部门、用户组和原生 PolarRAG 主体。 |
| 绑定企业身份 | `POST /api/identity-sources/users/{user_id}/identities` | 路径参数 `user_id`；请求体 `identity_source_id`、`external_user_id` | 将已同步企业用户映射到已有 PAS 用户；若该企业身份当前属于其自动创建的 PAS 用户，则安全转移映射。 |
| 修改映射 | `PUT /api/identity-sources/users/{user_id}/identities/{identity_id}` | 路径参数 `user_id`、`identity_id`；请求体 `identity_source_id`、`external_user_id` | 用另一已同步企业用户替换手工维护的映射。自动同步用户自身的主身份不可编辑。 |
| 删除映射 | `DELETE /api/identity-sources/users/{user_id}/identities/{identity_id}` | 路径参数 `user_id`、`identity_id` | 移除手工维护的映射并恢复企业身份的默认同步用户。自动同步用户自身的主身份不可删除。 |

`external_user_id` 必须从身份源目录接口返回的稳定外部用户 ID 中选择，不能使用邮箱或展示名猜测。

## 飞书 ACL 成员快照

| 操作 | 方法和路径 | 请求参数 | 作用 |
| --- | --- | --- | --- |
| 配置成员快照 | `PUT /api/identity-sources/{source_id}/acl-membership-snapshot` | 路径参数 `source_id`；请求体 `host`、可选 `port`、可选 `database`、`username`、`password` | 为已验证飞书来源保存 ACL 成员快照连接。更新后来源回到待绑定状态，需再次同步。 |

该接口仅用于确有 ETL ACL 成员快照的飞书部署；正常飞书或 SharePoint 目录同步不需要调用。密码为只写字段，必须由受保护的自动化环境提供。
