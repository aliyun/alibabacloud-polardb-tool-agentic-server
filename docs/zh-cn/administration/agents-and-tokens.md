# Agent 与 Token

[English](../../en/administration/agents-and-tokens.md)

Agent 是非人类 MCP 身份，拥有独立的状态、Token、实例直连绑定、供应绑定和
自有资源。

PAS 提供两种相互独立的 Agent 相关凭证：

- `pas_agent_` 代表机器 Agent，可以获得数据库 Tool；
- `pas_user_agent_` 代表“一个明确分配的 PAS 用户 + 一个 Agent”，可以获得
  所选 Space 支持且授权允许的 PolarRAG 读取、管理和上传 Tool。

`pas_agent_` Token 不能冒充用户，也不能调用 PolarRAG Tool。

## 创建与连接

创建 Agent 时填写清晰的名称和用途。详情页展示有效 Token、MCP 服务 URL 和
JSON 客户端配置，其中 MCP Server 名称默认使用 Agent 名称。只复制到预期的
客户端。

管理员视图会直接展示有效 Token，便于运维配置。应把该页面访问视为密钥访问，
不要把 Token 截入截图、工单或日志。

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

管理员在 Agent 详情页的 **PolarRAG 实例** 页签绑定允许访问的 PolarRAG 实例，
并明确分配 PAS 用户。管理员只能查看分配关系和 Token 状态，也可以强制吊销，
但永远看不到用户 Token 明文。

被分配的用户登录后，在 **My Instances** 的 **MCP connections** 中签发、查看、
重新生成或吊销自己的 Token。每个分配关系最多有一个有效 Token。最终知识
范围是 Agent 绑定实例、PAS 用户可见资源和 PolarRAG 文档 READ 判定的交集。

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
