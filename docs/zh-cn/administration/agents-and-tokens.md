# Agent 与 Token

[English](../../en/administration/agents-and-tokens.md)

控制台将员工归入**访问管理 → 个人账号**，Agent 归入**服务账号**。通过**资源**管理数据库授权，通过**连接 MCP**直接以个人身份访问，无需 Agent。下文的既有高级配置和 Agent 连接继续支持。参见[账号与资源](accounts-and-resources.md)。

Agent 是非人类 MCP 身份，拥有独立的状态、Token、实例直连绑定、供应绑定和
自有资源。

PAS 提供两种相互独立的 Agent 相关凭证：

- `pas_agent_` 代表机器 Agent，可以获得数据库 Tool；
- `pas_user_agent_` 代表“一个明确分配的 PAS 用户 + 一个 Agent”，可以获得
  所选 Space 支持且授权允许的 PolarRAG 读取、管理和上传 Tool。

`pas_agent_` Token 不能冒充用户，也不能调用 PolarRAG Tool。

## 创建与连接

创建 Agent 时填写清晰的名称和用途。详情页展示有效 Token 状态、MCP 服务 URL，
并提供 Token 和 JSON 客户端配置的复制操作，其中 MCP Server 名称默认使用
Agent 名称。只复制到预期的客户端。

复制操作使用当前已认证管理员 Session 读取有效 Token，不会再次要求输入 PAS
密码；读取仍受审计和限流保护。控制台会把密钥直接写入剪贴板，在受控私网 HTTP
页面上也会使用降级复制，并且不会在页面中显示明文。应把 Agent 详情页和未锁定的
管理员 Session 视为密钥访问，不要把 Token 截入截图、工单或日志。

## Token 生命周期

重新生成会立即使旧 Token 失效；吊销会阻止认证，直到签发新 Token。停用
Agent 会独立于 Token 状态阻止新操作。已有 MCP Session 可能保留旧工具目录，
因此状态、Token 或绑定变化后应重新连接。

用户签发或重新生成 `pas_user_agent_` Token 时，`expires_at` 为可选项；留空会
创建没有固定过期时间的 Token。PAS 不增加 `issued_at`，也不会因空闲而使该 Token
过期；`last_used_at` 只用于遥测。显式到期、吊销、用户或 Agent 停用以及 Agent
分配关系撤销，都会在下一次认证请求立即生效。
显式 `expires_at` 必须带时区偏移。PAS 会在持久化前转换为 UTC；不带偏移的
datetime 会被拒绝。

## 访问绑定

直连绑定选择已注册实例、凭证、权限和能力。SQL 代理能力是可选项，可以开放
`sql:read`，并在 `readwrite` 下开放 `sql:write`。供应绑定只适用于健康的
`multitenant` 后端，可以在没有直连 SQL 权限时单独开放
`db_instance:create`。

已经绑定到 Agent 的实例不会再次出现在新绑定选择器中。需要调整时应删除或
编辑已有绑定，而不是创建重复绑定。
Agent 详情页按 **数据库实例** 和 **PolarRAG 实例** 两个页签管理。两个页签为
方便使用都会展示同一份 MCP 连接信息，底层仍共用同一个 Agent Token 和 MCP
端点。数据库绑定、REST 连接信息、供应路由和资源归入数据库页签；PolarRAG
实例绑定以及用户、组分配归入 PolarRAG 页签。

## PolarRAG 用户连接

常规配置直接在 Agent 详情页的 **PolarRAG 实例**页签点击**配置企业访问**。选择一个有效
身份源、指定组或同步 PAS 用户、活动的 PolarRAG 实例，并从每个所选实例中至少选择一个
已启用 Space。确认预览时会在同一事务中创建缺失的 Agent-实例绑定。只有需要收窄默认范围时，
才使用高级控件配置 PUBLIC 范围。**全部同步用户**展示在首位但绝不会默认勾选，必须由管理员
显式选择。

预览会区分 Agent 局部授权、新增的全局 Source-Space 绑定以及已配置并复用的关系。确认缺失
的 Source-Space 绑定会改变全局共享状态。从当前 Agent 删除用户、组或全部同步用户授权时，
只删除该 Agent 授权；全局 Source-Space 绑定、Agent-实例绑定和共享 PUBLIC 范围都保留。
既有实例、PUBLIC 范围、用户、组和删除控件仍可用于高级管理。

Agent 列表、已分配/未分配用户和组选择器、PolarRAG 实例选择器、Space 以及 PUBLIC
知识资源选择器都使用后端搜索与分页；修改搜索词会回到第一页。即使租户包含大量
用户、组、Space 或知识库，Agent 详情页和企业访问弹窗也不会先加载全部对象。

管理员只能查看分配关系和 Token 状态，也可以强制吊销，但永远看不到用户 Token 明文。

被分配的用户登录后，在 **My Instances** 的 **MCP connections** 中签发、查看、
重新生成或吊销自己的 Token。复制操作使用当前已认证的 builtin 或 SSO Session，
不会再次要求输入密码；读取仍受本人归属校验、审计和限流保护。每个分配关系最多
有一个有效 Token。最终知识范围是 Agent 绑定实例、PAS 用户可见资源和 PolarRAG
文档 READ 判定的交集。

Agent 初始使用 `LEGACY_ALL` 模式。在 `SCOPED` 模式下，PAS 将所有命中的用户直属范围
和组范围取并集，再与 Agent 全局 PolarRAG 上限及用户可见资源取交集。手工范围与外部
同步范围可以并存；管理员可完整替换范围，以修改任意用户、组或知识库绑定，无需重新
签发 Token。PolarRAG 文档 ACL 仍是最终授权门禁。
Agent 详情页的**知识库绑定**区域会展示这些关系。管理员可以在一次操作中把多个已分配
用户、Department 或身份源用户组绑定到多个 KB，也可以解绑任意所选子集；外部同步关系
仅展示，在 Dashboard 中保持只读。

Agent 绑定默认包含该 PolarRAG 实例各启用 Space 的全部 PUBLIC 知识资源。管理员可在
绑定上使用
**配置 PUBLIC 范围**，把范围收窄到指定的已同步 ACTIVE PUBLIC 资源；空选择表示
排除全部 PUBLIC 资源。保存为 **全部 PUBLIC 资源** 会恢复实例级默认范围，后续同步
且符合条件的 PUBLIC 资源也会自动进入范围。

PERSONAL 资源不会出现在管理员选择列表中，仍完全遵循 PAS 用户企业主体映射、
PolarRAG owner 规则和文档 ACL。PUBLIC 选择只是额外的访问上限：它可以从发现结果和
所有资源型 MCP Tool 中移除资源，但不能授予用户或文档 ACL 原本没有的权限。范围
修改会在现有用户专用 Agent Token 的下一次 Tool 调用实时生效，无需重新签发 Token
或重连 MCP。

## 复查

定期复查闲置 Agent、最后使用时间、自有资源，以及 Audit Logs 中的 SQL 和
PolarRAG 页签。停用客户端或人员自动化前先吊销 Token。
