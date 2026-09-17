# 配置模块参考

[English](../../en/reference/configuration-modules.md)

运行时配置以加密、带 revision 的模块文档存储在元数据库中。只有
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY` 保留为进程启动配置。

## 模块目录

- `token_security`：共享 JWT 密钥环和 Token 生命周期。
- `core_admin`：首个内置管理员，依赖 `token_security`。
- `agent_token_auth`：为 MCP 和 Agent REST API 签发并认证 Agent Bearer Token
  的必需内置能力，默认激活，与人类管理员认证无关，不能跳过或停用。
- `user_sso`：可选 OIDC 或 OAuth 2.0 UserInfo 人类登录及受信任外部 Access
  Token 验证，依赖 `token_security`。
- `aliyun_access`：按模式划分的阿里云凭证、地域和 `openapi_network`。
- **服务运行策略**（`runtime_policy`）：外部 URL、CORS、连接池、Worker
  策略、全局
  `delete_cooldown_duration_hours` 和 `dedicated_pool_enabled` 激活 Flag。
- **SQL 安全策略**（`sql_security`）：限制、阻止操作、确认、限流和 SQL 超时。
- **PolarRAG 运行策略**（`polarrag_tool_limits`）：PolarRAG MCP Tool
  的进程本地速率、突发、实例并发、扇出治理及上游请求超时。
- `observability`：应用日志与全局审计生命周期行为。

可选模块可以保持 `SKIPPED`。停用依赖项前先停用所有有效下游模块。

自动供给池的网络位置、容量、权限和路由是在 **Pool** 与 **Agent** 页面管理的
资源，不是配置模块。旧 `resource_pool` 模块已经退役，配置命令会拒绝该模块名。

服务端内置的 `agentic-dedicated-mysql` profile 是 AgenticDB Dedicated 集群固定
`CreateDBCluster` 参数的唯一来源。资源池选择网络位置及 profile 允许的存储类型；
管理员不再维护第二套购买参数模块。

## 自动供给 Worker 运行安全

**服务运行策略**（`runtime_policy`）模块提供以下自动供给 Worker 控制项：

- `dedicated_pool_enabled` 启动或停止真实 Dedicated 生命周期任务。每个副本的
  进程内监督任务会在线应用有效 Revision，修改后无需重启或滚动 PAS。
- `dedicated_worker_heartbeat_interval_seconds` 默认为 10 秒。
- `dedicated_worker_heartbeat_stale_after_seconds` 默认为 30 秒，并且必须同时不少于
  三个心跳间隔和 30 秒。心跳写入和新鲜度比较使用元数据库时钟。
- `dedicated_pool_simulation_enabled` 默认为 false。它是显式的开发/测试回退开关，
  仅在没有有效阿里云凭证时创建带标记的模拟成员；有效凭证始终优先。该值为 false
  时，缺少阿里云访问配置会 fail closed；生产环境不得保留该开关。
- `dedicated_pool_preparation_mode` 默认为 `full`。`openapi_only` 会创建真实且
  计费的云资源，但在私网 MySQL 授权与验证前停在持久化 `OPENAPI_READY`。切回
  `full` 后，无需重启 PAS 或重复购买，Worker 会在线继续这些成员。

在这个暂停边界，资源池成员保持 `REPLENISHING`。如果该成员来自 Agent 冷创建
请求，Agent 侧数据库资源也保持 `CREATING`，不得返回凭证或参与分配；底层
PolarDB 集群仍然已经运行并产生费用。

控制台就绪接口会报告持久化的活跃 Worker 证据、模拟模式和准备模式。新启用的 Worker 可能需要
一个心跳间隔才会显示为活跃。启用模块并不能证明其阿里云身份具有
`CreateDBCluster` 权限。

## PolarRAG 运行策略

`polarrag_tool_limits` 与 `runtime_policy`、`sql_security` 均相互独立。
服务运行策略控制 PAS 全局连接池、配置轮询和 Worker；PolarRAG 运行策略只控制
PolarRAG 上游请求超时以及 MCP Tool 准入与上游扇出。安全默认值如下：

- `enabled: true`；
- `user_requests_per_minute: 60`（范围 `1..60000`）和 `user_burst: 10`
  （范围 `1..10000`）；
- `agent_requests_per_minute: 120`（范围 `1..60000`）和 `agent_burst: 20`
  （范围 `1..10000`）；
- `instance_max_inflight: 16`（范围 `1..10000`）；
- `max_fanout: 8`（范围 `1..1000`）；
- `max_exhaustive_knowledge_resources: 1000`（范围 `1..10000`），用于限制
  单次绑定快照或穷举搜索中的知识资源数量；
- 并发和扇出拒绝使用 `retry_after_seconds: 1`（范围 `1..3600`）；
- 每个 PolarRAG HTTP 请求使用 `upstream_request_timeout_ms: 20000`
  （范围 `100..300000`）。

每次请求同时检查 PAS 用户；使用用户专用 Agent Token 时还检查 Agent。实例并发
以 `polarrag_instance_id` 为键。所有限额都保存在进程内存中，在每个 PAS 副本
独立生效；部署 `N` 个副本时，聚合容量约为配置值的 `N` 倍。这不是跨副本精确
分布式限流。

管理员在现有服务配置页编辑并激活该模块。有效修改会在正常运行时配置轮询周期后
动态生效，无需重启 PAS。修改速率或突发值会重置本副本 Token Bucket；已运行的
上游调用会持有预约直到完成，新预约使用当前有效限额。请求超时同样会在轮询周期后
用于新建客户端；在途请求保留其客户端创建时的超时。该配置不是覆盖多请求 Tool
调用全链路的单一截止时间。

## 全局审计可观测性

**可观测性**（`observability`）模块除了应用日志级别、输出、轮转和时区外，还管理
全局审计设置：

- `audit_enabled: true` 控制可选审计记录；设为 false 时，安全必需的审计事件仍会
  记录；
- `audit_retention_days: 180`（最小值 `1`）控制所有数据库审计记录的清理周期。

日志轮转字段只控制应用日志文件，与审计保留时间没有重复。存量安装启动时会迁移
原先位于 `sql_security` 的有效审计配置，不会把自定义值重置为默认值。

## 工作流状态

生命周期包括 `NOT_CONFIGURED`、`DRAFT`、`VALIDATING`、`VALIDATED`、
`ACTIVE`、`ERROR`、`DISABLED` 和 `SKIPPED`。编辑只创建草稿，不改变有效
快照。验证生成与 revision、规范化摘要和依赖 revision 绑定的短期凭据。
激活必须携带该凭据和预期 revision。

## 外部验证

只有依赖外部服务的模块执行网络 I/O。对于 `aliyun_access`，后端 Pod 发送
只读 PolarDB 元数据请求，AssumeRole 模式先调用 STS。结果只包含解析出的
端点/状态和脱敏失败代码，绝不包含凭证或原始 SDK 异常。

`openapi_network` 只接受 `public` 或 `vpc`，自定义主机名会被拒绝。

对于 `user_sso`，OIDC Discovery 提供可信 issuer。使用手工端点配置时必须
显式提供 `issuer`、`authorization_endpoint` 和 `token_endpoint`。OIDC 模式
会按该 issuer 精确验证 ID Token；OAuth 2.0 UserInfo 模式支持不返回 OIDC ID
Token 的 Provider，但必须存在有效 UserInfo Endpoint。

### 用户 SSO 字段与激活

`user_sso` 接受以下字段：

- `browser_login_enabled`：默认为 `true`；纯 Token Exchange 部署时设为
  `false`；
- `protocol_mode`：`oidc` 或 `oauth2_userinfo`；
- Provider 连接：`discovery_url`，或手工 `issuer`、
  `authorization_endpoint` 和 `token_endpoint`；
- `userinfo_endpoint`：OIDC 模式可选，OAuth 2.0 UserInfo 模式必需；OIDC
  模式还必须存在有效 `jwks_uri`；
- `client_id`、加密保存的 `client_secret` 和 `scopes`；
- `provider_name`、`user_id_claim`、`display_name_claim`、
  `email_claim` 和 `default_department`；
- `id_token_algorithms`、`idp_pkce` 和 `userinfo_token_method`
  （`bearer_header`、`form_post` 或 `query`）；
- `external_token_trust`：包含外部 Provider 适配器、可选预期 audience、PAS
  Access Token 生命周期，以及单独控制的 MCP 直连兼容开关。

验证要求公网 HTTPS 地址、配置与 Discovery Issuer 完全一致、JWKS 可访问且
包含非空 `keys` 数组；请求超时为 10 秒，不跟随重定向，JSON 响应不超过
1 MiB。正常运行时会拒绝私网、回环、链路本地及其他不安全目标地址。同机开发
可使用 `pas serve --local-sso-dev`，此时 PAS 外部基础 HTTP Origin 仅允许
与 Listener 匹配的 `localhost` 或 `127.0.0.1`；Provider HTTP 端点还可以
使用 `::1`。PAS 所有 Listener 绑定到 `127.0.0.1`。局域网、通配、其他
`127/8`、相似域名以及解析到非回环地址的目标仍会被拒绝。回调地址只根据
`runtime_policy.external_base_url` 生成：

```text
EXTERNAL_BASE_URL/auth/oidc/callback
```

当 `browser_login_enabled=false` 时，PAS 进入纯 Token Exchange 模式，不再要求
回调 URL、Discovery/授权/Token/JWKS 端点、浏览器 Client ID 与 Secret，也不要求
浏览器登录测试；但仍必须配置 HTTPS External Base URL，用于标识 Token Exchange
Resource。此时必须启用外部 Token 信任，并选择
`oauth2_introspection`、`oauth2_userinfo` 或 `feishu`；Provider 校验通过后即可
直接激活。Introspection 必须配置独立的 PAS-to-provider `client_id` 和
`client_secret`，独立 UserInfo 必须配置
`external_token_trust.userinfo_endpoint`。

纯 Token Exchange 模式下，普通控制台继续使用内置登录；启用浏览器登录时，仅通过
网络验证还不能激活，激活后普通控制台登录使用 SSO。
管理员调用
`POST /api/config/user-sso/tests`，在返回的浏览器授权地址完成登录，再轮询
`GET /api/config/user-sso/tests/{id}`。激活 `user_sso` 时，除常规
`validation_id` 和 `expected_revision` 外，还必须携带已通过测试的
`sso_test_id`。该证明只能使用一次，并绑定管理员、revision、规范化摘要和
有效期。

独立恢复入口只接受活动的内置管理员，并继续执行限流和审计。

### 外部 Access Token 信任字段

`external_token_trust.enabled` 允许已注册 Client 向 `/token` 发送 RFC 8693
请求，Authorization Server Metadata 会发布 Token Exchange Grant。
`provider` 选择一个适配器：

- `oidc_jwt` 使用 SSO issuer、Discovery/JWKS、算法、Claims 和 Client ID；
  `expected_audience` 可覆盖 Client ID，作为 JWT 必须匹配的 audience。
- `oauth2_introspection` 使用 `client_secret_basic` 或
  `client_secret_post` 调用 `introspection_endpoint`；配置了
  `expected_audience` 时，它必须出现在活动响应中。嵌套的 `client_id` 与
  `client_secret` 可配置 PAS 调 Provider 的独立凭证；未配置时沿用浏览器 SSO
  凭证。配置 `userinfo_endpoint` 后，PAS 使用外部 Bearer Token 调用它，并要求
  返回 `sub`、`user_id` 和 `pas_source_id`，可选返回 `union_id`；UserInfo
  的 `sub` 必须与 Introspection 的 `sub` 一致。此模式还必须通过
  `identity_source_id` 选择包含该 `user_id` 的活动且已验证企业身份源。
- `oauth2_userinfo` 使用有效 `userinfo_endpoint`、
  `userinfo_token_method` 和已配置的身份 Claims。
- `feishu` 兼容旧的静态 `identity_source_id` 配置，但 Token Exchange 调用方也可在
  每次请求中传入 `identity_source_id`、`feishu_user_id` 和
  `feishu_union_id`。三个直接传入字段必须同时存在；PAS 加载活动且已验证的飞书
  身份源，并在签发 Token 前将它们与飞书 UserInfo 的返回值逐项比对。`/mcp` 没有
  对应的表单字段，不能使用请求身份上下文；飞书 MCP 直连仍要求旧的固定身份源。
- `buc` 要求 `protocol_mode=oauth2_userinfo`、
  `userinfo_token_method=form_post` 且 `user_id_claim=account_id`；PAS 还
  要求返回的 `client_id` 与配置的 Client ID 相同。

`access_token_ttl_seconds` 默认为 `28800`（8 小时），范围为 `60..86400`；PAS Access
Token 的实际生命周期不会超过已知的外部 Token 到期时间。
`direct_mcp_enabled` 默认是 `false`，且只有启用外部信任后才能打开。打开后，
`/mcp` 收到无法识别的 Bearer Token 时，会在每次请求中按外部 Token 验证。
直连路径会映射 PAS User 与 Workspace，但不返回 PAS Access Token 或 Refresh
Token。标准 Token Exchange 同时支持 `/token` 和
`/api/v1/external-auth/token`，只返回 PAS Access Token，不返回 Refresh Token。

当 Introspection 与外部 UserInfo 端点解析出的地址全部位于允许的私网网段时，
可以使用 HTTP，以支持隔离的 VPC 和企业内网部署。此时 Provider 凭证与外部
Token 在传输中不会加密，仍建议优先使用 HTTPS；公网端点必须使用 HTTPS。
链路本地地址、云元数据地址、组播、未指定、保留地址、公私网混合解析以及未经
允许的回环地址仍会被拒绝。浏览器 SSO 端点继续执行更严格的公网 HTTPS 策略。
请求仍受超时、禁止重定向和响应大小限制。Provider 凭证与外部 Token 不会从
Describe/Export API 返回，也不会写入校验日志。

该 Provider 配置激活后，管理员在**外部应用**中注册调用方服务。PAS 会创建机密
OAuth Client，根据 External Base URL 生成 MCP 与 HTTP API Resource Profile，
应用所选 Agent 策略，并且只在创建或轮换时显示 Client Secret。Provider 配置
决定 PAS 如何验证外部 Token；外部应用注册决定哪个调用方可以执行交换，以及它
可以访问的 PAS 目标和 Agent 选择规则。
验证尚未激活时，**外部应用**页面会直接打开 `user_sso` 模块，并展开
**外部 Access Token 信任**。

## 阿里云凭证模式

`aliyun_access` 只有以下三种模式：

- `direct_ak` 直接使用已加密保存的长期 AccessKey ID 和 Secret。
- `assume_role` 在 PAS 加密保存低权限源 AccessKey ID 和 Secret，再扮演
  RAM 角色，使用临时身份凭据（STS Token；SDK 文档中也称
  `temporary credentials`）发起 OpenAPI 调用。
- `ecs_ram_role` 不保存 AccessKey。PAS 必须部署在已授予该 RAM 角色的
  ECS 实例上。运行时仅使用 IMDSv2；不支持 IMDSv1 回退、任意元数据 URL
  或第二段角色扮演链。

运行时只解密选中的块。临时凭证只留在进程内存，绝不会持久化、出现在 API
响应、导出或日志中，也不会发送给 Agent 或 sandbox。AccessKey ID 在静态保存时
同样会加密，并使用服务端生成的短 `display mask`；AccessKey Secret 和 External
ID 只显示“已配置”状态。dry run 结果可以包含脱敏身份或角色、临时到期时间和安全
的阿里云 Request ID。

当当前模式存在已保存凭据时，切换模式需要 confirmation。默认选择是
**Clear the previous credential**。
**Retain it, but keep it disabled** 会加密保留旧块但使其失效；该块不会被解密或
自动复用。要重新激活时，选择 **Use retained credential** 或输入替换值，然后
验证并显式启用。**Delete retained credential** 会永久删除该非活动块。将保留的
direct AccessKey 复用为 AssumeRole 的源凭证同样是独立、需要确认的操作。

`direct_ak` 的 dry run 只读取 PolarDB 元数据。`assume_role` 会先执行
AssumeRole，再使用临时凭证读取 PolarDB。`ecs_ram_role` 会先经由 IMDSv2 获取
ECS 角色，再进行同样的只读检查。PolarDB 预检调用 `DescribeDBClusters`，只能
证明凭据具备集群查询权限；它不会验证自动供给池购买所需的
`CreateDBCluster` 等权限。启用自动供给前请授予这些权限，否则资源池可能
无法自动创建实例。
`OPENAPI_PERMISSION_DENIED` 表示活动凭证已连接到 PolarDB 但缺少所需授权；
请按故障排查指南操作，不要盲目扩大权限。

## 密钥与导出

Secret 字段在根密钥下独立加密。省略已有 Secret 会保留原值，显式支持的清除
动作才会删除。Describe 和导出响应只包含已配置/脱敏标记。
