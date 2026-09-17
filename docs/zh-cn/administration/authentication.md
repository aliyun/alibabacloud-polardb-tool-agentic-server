# 认证

[English](../../en/administration/authentication.md)

PAS 将首次接管、人类认证和 Agent Token 分离。一种身份类型的凭证不能代替
另一种身份类型。

## 首次接管与内置登录

一次性 bootstrap token 只在系统处于 setup 模式时接受，并在
`core_admin` 激活时被消费。首个管理员随后使用内置登录。Session Cookie
要求 Web 控制台使用的 CSRF 请求头；API 客户端应使用受支持的 Bearer 流程。

保留的 `set_initial_password` 命令仅限首次调用：它从 `RESET_REQUIRED` 初始化内置管理员到
`ACTIVE`，后续尝试会返回 `ADMIN_PASSWORD_ALREADY_INITIALIZED`。一旦初始化完成，与业务监听器隔离、需要
显式启用的管理监听器支持固定 `admin` 账号的密码 `MODIFY`（必须提供当前密码）和密码
`RESET`（不提供旧密码）。`RESET` 在 `RESET_REQUIRED` 和 `ACTIVE` 两种状态下都有效。

每次成功的内置密码初始化、修改或重置都会递增凭证 epoch，吊销 REST 和 MCP OAuth
refresh token，使尚未使用的内置 authorization code 失效，并拒绝已有的密码认证访问
会话。任何端点都不会返回密码。不要把 bootstrap token 或密码放入 URL 或配置仓库。

## Web 控制台语言

Web 控制台支持英文（`en-US`）和简体中文（`zh-CN`）。首次访问时会跟随浏览器
语言；浏览器语言不受支持时回退到英文。可在登录、初始设置或已登录的控制台页面
使用语言切换器覆盖自动选择。手动选择会保存在浏览器中，并在后续访问时优先生效。

切换显示语言只影响前端标签、提示消息、Ant Design 组件以及本地化的日期或数字
格式。API 请求与响应、标识符、SQL 和后端诊断详情不会被翻译。

## 可选 SSO

`user_sso` 模块可以保持 `SKIPPED`。激活后，普通控制台登录页只显示已配置的
企业 SSO 入口。独立的 `/login/recovery` 路由为活动的内置管理员保留恢复入口；
恢复登录会被严格限流并写入审计，不是普通用户的第二种登录方式。

应先把 `runtime_policy.external_base_url` 配置为外部可访问的 HTTPS Origin。
SSO 表单会展示唯一需要在身份提供方注册的回调地址：

```text
https://PAS_HOST/auth/oidc/callback
```

没有公网 IdP、需要在同一台机器开发测试时，使用
`pas serve --local-sso-dev` 启动 PAS。只有在该显式模式下，外部基础 URL
才可以使用与 IPv4 Listener 匹配的 `localhost` 或 `127.0.0.1` HTTP 地址；
Provider 端点还可以使用 `::1`。PAS 所有 Listener 同时绑定到
`127.0.0.1`；远程、局域网、通配、其他 `127/8` 地址和相似域名仍会被拒绝。

优先使用 OIDC Discovery。手工模式必须提供 `issuer`、
`authorization_endpoint`、`token_endpoint` 和 `jwks_uri`，UserInfo
端点可选。保存草稿不会直接激活 SSO，管理员必须依次：

1. 保存并校验当前 revision；
2. 由同一管理员完成真实浏览器登录测试；
3. 使用该成功测试凭据激活同一 revision。

PAS 将测试凭据绑定到管理员、revision、规范化配置摘要、身份提供方 subject
和有效期。编辑草稿后测试凭据立即失效。激活时也会阻止把已经属于其他 PAS
用户的外部身份静默改绑。Client Secret 已配置时，留空会继续沿用加密保存的值。

校验由 PAS 后端获取 Discovery 和 JWKS，使用超时、响应大小限制、禁止重定向、
HTTPS 强制和公网地址检查。显式 localhost 开发模式会对其允许的 HTTP 端点
改用严格回环解析检查。校验同时验证 issuer 一致性及必需端点格式；Client
Secret 和提供方原始响应不会返回给浏览器。

控制台登录和 MCP 授权都会把 OIDC 回调绑定到短期、一次性的 state 与 OIDC
nonce；可选的 IdP 侧 PKCE 使用 S256。PAS 会校验 ID Token 的签名、issuer、
audience 和 nonce；同时读取 UserInfo 时，其 `sub` 必须与已验证 ID Token 的
`sub` 完全一致。成功回调会映射或 JIT 创建 PAS User，再由 PAS 签发自己的
浏览器 Session 或 OAuth Grant。在该浏览器流程中，身份提供方 Token 不是 MCP
凭证，PAS 也不会在每次 MCP 请求时把 Token 转发给身份提供方。管理员还可以
单独开启下文介绍的受信任外部 Access Token，用于 Token Exchange 或显式启用的
MCP 直连兼容模式。验证失败返回通用、不可缓存的认证错误，并消费一次性 state。

## MCP OAuth 安全契约

PAS 支持 OAuth 2.0 Authorization Code 与 Refresh Token Grant，并采用 OAuth
2.1 安全基线。MCP Client 与 PAS 之间强制使用 PKCE `S256`，不开放 implicit
和 resource owner password Grant。支持 OAuth 的 MCP Client 可以使用动态
客户端注册或预注册 Client；公共 Client 使用 `none`，机密 Client 可使用
`client_secret_basic` 或 `client_secret_post`。

每个 redirect URI 都会原样保存并精确比较。远程回调必须使用 HTTPS；HTTP
只允许 `localhost`、`127.0.0.1` 或 `::1` 回环客户端，回环注册地址可以匹配
客户端启动时选择的临时端口。包含凭证、fragment、通配符、自定义 scheme 或
未注册 redirect URI 的请求都会被拒绝。

PAS 会发布 Protected Resource 与 Authorization Server Metadata。客户端从
`/mcp` 发现授权服务器，打开系统浏览器完成 SSO，交换 authorization code，
并刷新 PAS Token。最终 MCP 请求仍然携带
`Authorization: Bearer <PAS access token>`；变化在于该凭证由客户端自动获取和
刷新，用户无需复制粘贴。OAuth `resource` 与 access token 的 `aud` 都指向 PAS
的 `/mcp` 端点。

PAS 签发的 MCP access JWT 强制包含 `iss`、`aud`、`sub`、`iat`、`exp`、`jti`
和 `type`。`iss` 必须精确等于 PAS 外部基础 URL，`aud` 必须等于 `/mcp` 资源
URL。每次请求都会校验签名、issuer、audience、过期时间、subject、唯一 token ID
以及 `type=access`。issuer 只存在于 JWT 内，不会增加到复制的 MCP JSON 中。

PAS 会在校验凭证或 grant 前执行 Pod 本地限流：同一账户每分钟最多尝试
builtin 登录 5 次，同一远端地址每分钟最多尝试 builtin 登录 20 次、动态注册
10 次、token 请求 20 次。超过限制时返回 HTTP `429` 和 `Retry-After`。这些是
最小防护，不是分布式集群配额；多副本部署还应在 Ingress 增加统一保护。

## 受信任外部 Access Token

活动的 `user_sso` 模块可以信任一个外部 Access Token Provider。当前支持以下
验证适配器：

- OIDC JWT Access Token：校验 `at+jwt` 类型、签名、issuer、audience、过期
  时间、subject、client ID、签发时间和 Token ID；
- OAuth 2.0 Token Introspection：要求响应中 `active=true`，并可校验预期
  audience；
- OAuth 2.0 UserInfo：根据 Provider 要求，通过 Authorization Header、表单
  Body 或 Query 参数传递 Access Token；
- 飞书 User Access Token：按每次 Token Exchange 请求指定的活动且已验证飞书企业
  身份源及其租户进行校验；
- BUC Access Token：使用表单 POST UserInfo，并校验 `client_id` 和
  `account_id`。

推荐使用标准 OAuth Token Exchange。管理员应在一个入口完成接入准备：

1. 在**服务配置 > 用户单点登录**中，如果只使用 Token Exchange，关闭
   **启用浏览器 SSO 登录**，再配置、校验并激活外部 Access Token Provider；
   此模式不需要回调地址、浏览器端点或浏览器 Client 凭证。
2. 打开**外部应用**并注册调用方服务。
3. 选择允许访问的目标（MCP、API 或两者）以及 Agent 策略。
4. 将页面展示的 `client_id` 和仅显示一次的 `client_secret` 保存到调用方
   服务的密钥管理系统。
5. 从同一页面复制 Token Endpoint、Resource、Scope 和请求示例；应用详情页
   是这些接入参数的唯一可信来源。

PAS 为每个管理员创建的外部应用生成机密 OAuth Client，并根据
`runtime_policy.external_base_url` 固定生成以下 Resource Profile：

| 目标 | Resource | Scope |
| --- | --- | --- |
| MCP | `<PAS base URL>/mcp` | `mcp` |
| HTTP API | `<PAS base URL>/api/v1` | `polarrag` |

应用只启用一个目标时可省略 `resource`，PAS 会选择唯一 Profile；同时启用两个
目标时必须传入 `resource`。调用方可以省略 `scope`，PAS 会根据 Resource 自动
推导；若传入，则必须与 Profile 完全一致，不能申请任意 Scope。

标准 Endpoint 是 `/token`；`/api/v1/external-auth/token` 是等价的兼容别名：

```http
POST /token
Authorization: Basic base64(PAS_CLIENT_ID:PAS_CLIENT_SECRET)
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange&
subject_token=EXTERNAL_ACCESS_TOKEN&
subject_token_type=urn:ietf:params:oauth:token-type:access_token&
resource=https%3A%2F%2FPAS_HOST%2Fmcp&
scope=mcp
```

当外部 Token Provider 选择**飞书 User Access Token**时，`subject_token` 是用户的
飞书 `user_access_token`。每次兑换还必须且只能传入一次以下 PAS 扩展参数：

```text
identity_source_id=PAS_VERIFIED_FEISHU_SOURCE_ID&
feishu_user_id=FEISHU_USER_ID&
feishu_union_id=FEISHU_UNION_ID
```

PAS 使用 `subject_token` 调用飞书 UserInfo，并要求返回的 `tenant_key`、`user_id`
和 `union_id` 分别与传入身份源及字段完全一致。飞书 Token 过期、身份源未启用或
不属于该租户、任一字段不一致时均返回 `invalid_grant`。PAS 不持久化飞书 Token，
也不维护 PAS Token 到飞书 Token 的映射；签发后的 PAS Token 按 PAS 配置的有效期
生效。

PAS 返回自己的 Access Token，不签发 Refresh Token，并返回
`issued_token_type=urn:ietf:params:oauth:token-type:access_token`。在
`/token` 请求中省略 `grant_type` 不会自动推断为 Token Exchange。

所选 Agent 策略决定 `agent_id` 的使用方式：

| 策略 | 请求行为 |
| --- | --- |
| Workspace 默认 Agent | 不得传 `agent_id`，PAS 使用用户 Workspace 默认 Agent。 |
| 固定 Agent | PAS 始终使用配置的 Agent；若传入，则必须与配置值一致。 |
| 调用方可选 | 调用方可选择一个已授权 Agent；省略时使用 Workspace 默认 Agent。 |

外部应用页面支持在交付凭证前使用真实外部 Subject Token 测试接入。选择飞书
Provider 时，测试弹窗还会要求填写 PAS 身份源 UUID、飞书 user ID 和飞书
union ID，并分别以 `identity_source_id`、`feishu_user_id` 和 `feishu_union_id`
提交。轮换 Secret 会立即使旧 Secret 失效；禁用应用会拒绝新的交换。
`client_secret` 只在创建应用或轮换 Secret 时显示一次。

兼容客户端仍可使用动态客户端注册。声明 Token Exchange 的 DCR Client 必须
注册至少一个 Scope：

```http
POST /register
Content-Type: application/json

{
  "token_endpoint_auth_method": "client_secret_basic",
  "grant_types": [
    "urn:ietf:params:oauth:grant-type:token-exchange"
  ],
  "scope": "mcp polarrag",
  "client_name": "External service"
}
```

公共 DCR Client 使用 `none`；机密 DCR Client 按注册信息使用
`client_secret_basic` 或 `client_secret_post`。纯 Token Exchange DCR Client
可省略 `redirect_uris` 和 `response_types`。服务到服务接入推荐使用管理员管理
的**外部应用**流程，因为该流程还会统一管理目标、Agent 策略、生命周期和可复制
的接入参数。

Token Exchange 失败统一返回 OAuth 错误结构
`{"error":"...","error_description":"..."}`：

| HTTP | Error | 含义 |
| --- | --- | --- |
| `400` | `invalid_request` | 必填参数缺失、重复或格式不合法。 |
| `400` | `unauthorized_client` | PAS OAuth Client 未注册 Token Exchange Grant。 |
| `400` | `invalid_grant` | Provider 拒绝 Assertion（包括飞书 Token 过期或身份不一致），或 PAS 无法映射对应用户。 |
| `400` | `invalid_scope` | 请求的 Scope 不适用于目标 Resource。 |
| `400` | `invalid_target` | Resource、指定 Agent 或默认 Agent 无效或未授权。 |
| `401` | `invalid_client` | 外部应用 Client 鉴权失败。 |
| `413` | `invalid_request` | 表单 Body 超过 Token Endpoint 大小限制。 |
| `429` | `rate_limit_exceeded` | Token Endpoint 请求超过限流。 |
| `503` | `temporarily_unavailable` | Provider Introspection 或 UserInfo 暂时不可用。 |

使用 Introspection 适配器时，PAS 先把外部 Assertion 作为 `token`
参数提交到配置的 Introspection Endpoint，`active=true` 表示 Token 鉴权成功。
之后 PAS 可使用 `Authorization: Bearer <external-access-token>` 调用单独配置的
`user_info` Endpoint。响应必须包含 `sub`、`user_id` 和
`pas_source_id`，可选包含 `union_id`；其中 `sub` 必须与 Introspection 响应
完全一致。当前 Introspection 身份映射要求 Source ID 对应配置的活动且已验证
企业身份源，并且 `user_id` 必须已在该身份源中映射为 PAS User。Provider Scope
保留在 Provider 自己的权限命名空间，不会被解释为 PAS 的 `mcp` 或
`polarrag` Scope；PAS 权限由外部应用、Resource、Workspace 和 Agent 授权
共同控制。

PAS 会确保 User Workspace 已创建。PAS Access Token 默认有效 28800 秒（8 小时），可配置
为 60 至 86400 秒，并且不会超过已知的外部 Token 到期时间。Provider 临时不
可用时返回临时错误，不会把 Token 当成已失效。

仅当 Introspection 与外部 UserInfo 端点全部解析到允许的私网网段时，才可以
使用 HTTP。该能力面向可信、隔离的企业内网部署；由于 PAS 调 Provider 的凭证
和外部 Token 会以未加密方式传输，仍建议优先使用 HTTPS，公网端点则必须使用
HTTPS。链路本地地址与云元数据目标仍会被拦截。此例外不会放宽交互式浏览器
SSO 端点的 HTTPS 要求。

对于只能向 `/mcp` 发送 Bearer Token 的存量 Client，管理员可以单独开启外部
Token MCP 直连。该兼容模式默认关闭。PAS 会在每次请求中验证和映射外部 Token，
不会签发 PAS Refresh Token，也绝不会把外部 Token 转发给 Agent、Tool、MySQL
实例或 PolarRAG 服务。Client 能调用 `/token` 时应优先使用 Token Exchange；
PAS 签发 Access Token 后，普通 MCP 请求路径不再依赖 Provider 校验。

## 个人 MCP 与旧 Workspace 访问

个人 Web 资源发现和 `/mcp/personal` 直接解析认证 User，无需 Agent。个人 Token 使用个人 audience，不授权控制台 REST 或旧 `/mcp`。参见[账号与资源](accounts-and-resources.md)。

下面的 Workspace 行为适用于现有 `/mcp` 与外部集成路径。

### 旧 Workspace 与 Agent 的关系

OAuth 或控制台身份首先映射为 PAS User。每个 User 拥有一个 Workspace，
Workspace 选择一个默认的活动 Agent。User 必须已通过直接分配、PAS 部门、
企业组或同步的身份源组获得该 Agent 的使用资格；Agent 的 MySQL 与 PolarRAG
绑定再决定用户可见的 Tool 和资源：

```text
User -> User Workspace -> Default Agent -> MySQL / PolarRAG bindings
```

因此 User 并不直接拥有一套数据库 MCP Tool。User 被授权使用 Agent，Agent
持有资源绑定；对于需要用户范围的 Tool，PAS 仍会继续应用 User 身份和 ACL。

只有一个可用活动 Agent 时，PAS 会自动选为默认值；有多个时，用户在
**我的实例 > MCP 连接**中选择。若默认 Agent 的授权随后被撤销，PAS 会保留
这个已失效的选择并拒绝 Tool 执行，直到用户显式选择其他 Agent，不会静默切换
资源。Workspace 状态固定为 `ready`、`selection_required`、
`no_agent_access` 和 `default_agent_unavailable`。

本版本不会为新用户自动创建 MySQL 或 PolarRAG 实例。管理员仍需授权 Agent
并绑定所需资源。现有 Agent Token 与 `pas_user_agent_` Token 继续兼容，
可供机器身份和手工配置的客户端使用。

## Agent 认证

每个 Agent 拥有一个独立管理的 Token。Token 在 `/mcp` 认证 Agent 身份，
不会创建管理员 Session。泄露后应吊销或重新生成，并重新连接 MCP 客户端，
以刷新工具列表和授权快照。

## 失败处理

连续认证失败应通过脱敏的应用日志和审计日志排查。不要要求用户在公开 Issue
中粘贴 Token、Cookie、OIDC Secret 或数据库 URL。
