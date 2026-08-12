# PolarRAG MCP 与企业身份

[English](../../en/knowledge/polarrag-mcp.md)

PAS 可以管理多个 PolarRAG 实例，并向经过认证的人类用户开放受 ACL 保护的知识
查询。该集成复用现有 PAS builtin 用户登录、User Token、MCP OAuth、元数据库、
审计日志和 `PAS_ENCRYPTION_KEY`。

## 安全边界

只有 subject 为 `user:<pas_user_id>` 的 MCP access token 才能看到并调用
PolarRAG Tool。机器 `pas_agent_` Token 不会获得这些 Tool，也不能冒充人类
用户。用户专用 `pas_user_agent_` Token 在服务端解析为被分配的 PAS 用户，同时
保留所属 Agent 的实例边界。

Tool 参数不能包含用户 ID、provider、身份域、principal、`acl_context`、
endpoint、凭证或 OpenSearch DSL。PAS 从可信 PolarRAG 目录取得 Space 身份域，
读取已认证用户的有效服务端主体映射，并构造 `acl_context`。PolarRAG 始终是文档
READ 权限的最终权威。

PAS 的资源发现只决定用户可以选择哪些知识库：

- active `PUBLIC` 知识库使用 `DOMAIN` 发现规则；
- active `PERSONAL` 知识库使用 `OWNER` 发现规则；
- 未知知识库类型和无法解析的 owner 均 fail closed。

资源发现不会授予文档权限，PAS 也不会查询 PolarRAG 元数据表或系统索引。

## Web 控制台

PolarRAG 管理能力统一放在现有 PAS 页面：

- 进入 **Instances**，点击 **Register Instance**，在 Engine 中选择
  `PolarRAG`，即可注册 endpoint 并保存共享 OpenSearch 账号和 TLS 设置。
  **PolarRAG Instances** 页签用于执行能力检查、轮换 write-only 凭证、停用
  实例，以及启用、同步或停用枚举得到的 Space。如果已启用 Space 中存在
  `UNCLAIMED` PERSONAL 知识库，管理员可在 Spaces 抽屉选择同一 identity domain
  下符合条件的 PAS 用户主体，然后点击 **Assign owner and activate**。PAS 会构造
  可信 actor 上下文，请求 PolarRAG 认领知识库，并同步 active 目录。PUBLIC 知识库
  创建后直接为 active，永远不进入认领流程。页面永远不会显示已保存的凭证和
  CA Bundle。
- 进入 **Users**，点击某个用户行的 **Enterprise Identity**，即可维护该用户在
  allowlist 内的飞书或 SharePoint user/group 主体。PAS Department 与外部企业组
  仍相互独立。
- 已认证用户可进入 **My Instances** 查看自己可访问的数据库实例和 PolarRAG
  知识资源。仅注册 PolarRAG 实例不会授予用户权限；管理员还必须启用并同步
  Space，并为该用户维护属于该 identity domain 的有效主体映射。管理员没有可访问
  的知识资源时，页面会说明控制面管理权限不会授予知识访问权限，并提供 PolarRAG
  管理页签链接；管理员身份不会绕过知识访问规则。
  非管理员用户可以向页面中 upload-ready 的知识资源上传本地文档。浏览器只提交
  opaque 资源 ID 和文件；PAS 根据已认证身份推导 Space、知识库、principals 和
  actor，PolarRAG 再根据 KB 策略和可信 actor 生成文档 ACL。用户还可通过
  **Manage documents** 按文件名查找文档并请求删除或 rechunk；
  PAS 继续传递同一份可信身份上下文，PolarRAG 分别执行 `MANAGE` 和 `EXECUTE`
  鉴权。管理员不能上传或管理文档。如果 **Upload** 按钮不可用，悬浮提示会要求用户
  联系管理员配置并验证该 Space 的 OSS AccessKey 凭据。PolarRAG 接收上传后，PAS
  留在 **My Instances** 显示接收结果。用户手动打开 **Manage documents** 时，页面按
  READ ACL 加载一页 20 个文档及其当前状态；**Previous**、**Next** 和 **Refresh**
  都由用户显式触发，PAS 不在后台轮询。PolarRAG 返回 chunk 数量时表格也会显示。
- 管理员在 Agent 详情页绑定允许的 PolarRAG 实例并分配 PAS 用户。被分配用户
  在 **My Instances > MCP connections** 管理自己的 `pas_user_agent_` Token。
  管理员只能查看状态和强制吊销，不能读取明文。
- 管理员 Dashboard 的 **Instances** 和 **Active** 统计同时包含已注册的数据库
  实例与 PolarRAG 实例；资源池可用数量仍只统计数据库实例。普通用户只会看到自己
  可访问的数据库实例数和 PolarRAG 知识资源数，不显示全局管理统计或管理操作。

旧 `/polarrag` 地址会跳转到 `/instances?type=polarrag`，侧栏不再保留独立入口。

上游插件无法提供可信 Space/知识库目录时，页面会显示
`POLARRAG_CATALOG_CAPABILITY_MISSING`。管理员不能手工输入 identity domain，也
不能绕过该能力检查。

## 注册实例

管理员路由位于 `/api/polarrag`：

- `POST /instances` 注册并检查一个 endpoint；
- `GET /instances` 和 `GET /instances/{id}` 返回脱敏配置；
- `PATCH /instances/{id}` 轮换共享账号、TLS 设置或名称；
- `POST /instances/{id}/check` 重新执行认证和能力检查；
- `DELETE /instances/{id}` 停用实例及其 Space；
- `GET /instances/{id}/spaces` 枚举可信上游 Space，并返回各 Space 已同步的知识
  资源、opaque resource ID、绑定模式和同步状态；
- `POST /instances/{id}/spaces/enable` 启用一个已枚举 Space，并立即同步其
  active 知识库；
- `DELETE /instances/{id}/spaces/{space_id}` 停用一个 Space，并立即隐藏其知识
  资源；
- `POST /instances/{id}/spaces/{space_id}/sync` 显式执行一次目录同步。
- `PUT /spaces/{knowledge_space_id}/oss-config` 为已启用 Space 验证并保存 OSS
  AccessKey 和对象前缀。bucket 与 endpoint 均为只读值，必须来自可信 PolarRAG
  Space 目录。
- `GET /instances/{id}/unclaimed-knowledge-bases` 列出已启用 Space 中的
  `UNCLAIMED` 知识库和符合条件的 owner 主体；
- `POST /instances/{id}/spaces/{space_id}/knowledge-bases/{kb_id}/claim`
  将同域的有效用户主体设为 owner，激活上游知识库、同步 Space 并写入审计记录。

创建请求包含 `name`、`scheme`、`host`、`port`、`username`、`password`、
`tls_verify` 和可选 `ca_bundle`。PAS 使用现有根密钥加密用户名、密码和 CA
Bundle，任何响应都不会返回这些值。PAS 还会每五分钟同步一次已启用 Space。

Space OSS 配置复用 `PAS_ENCRYPTION_KEY` 加密保存 AccessKey。保存配置时，PAS 使用
目录返回的 endpoint，在目录返回的 bucket 中写入并删除一个零字节探针，因此凭证
必须同时具备这两种权限。PAS 永远不会返回 AccessKey。上游 bucket 或 endpoint 一旦
变化，已有配置会立即失效，必须重新验证。

`GET /api/me/resources` 为 **My Instances** 页面返回已认证用户可访问的数据库
实例和 PolarRAG 知识资源。该接口复用 MCP 的服务端访问控制和资源发现规则，
不会返回 endpoint 凭证或可信 ACL 上下文。

Agent 范围的用户连接使用以下附加接口：

- 管理员维护 `/api/agents/{agent_id}/polarrag-bindings` 和
  `/api/agents/{agent_id}/user-assignments`，并可强制吊销分配关系的 Token；
- 用户通过 `/api/me/agent-connections` 列出连接，并对自己的分配关系执行
  `issue`、`reveal`、`regenerate` 和 `revoke`。

有效资源集合是 Agent 绑定的 PolarRAG 实例与现有用户发现规则的交集；每个
文档的 READ 判定仍由 PolarRAG 完成。

`POST /api/me/polarrag/documents` 接受一个 multipart 文件和一个 opaque
`knowledge_resource_id`，文件上限为 100 MiB。只有能够发现该资源的非管理员用户
可以调用。PAS 先上传到已验证的 Space bucket，再构造包含全部映射 principals 和
user actor 的可信 `acl_context`，不由调用方指定 `doc_id`，并使用
`acl.mode=POLARRAG_DERIVED` 把 OSS 路径提交给 PolarRAG。PolarRAG 校验
PUBLIC/PERSONAL KB 上传策略，返回权威文档 ID 并生成最终文档 ACL。若 PolarRAG
拒绝接收，PAS 会尝试删除暂存对象并返回脱敏错误。

用户文档控制台通过 `/api/me/polarrag/documents` 下的受保护分页列表、文件名查找、
删除和 rechunk 路由工作。每次操作只接受 opaque `knowledge_resource_id`；PAS 会验证
文档确实属于所选资源，且不接受调用方传入身份或 ACL 字段。成功的变更会写入现有
PolarRAG 审计分类。

Space 启用请求只接受 `space_id`。Space 名称和不可修改的 `identity_domain`
必须来自可信上游枚举响应，管理员不能手工输入或覆盖。
Spaces 抽屉会显示 Space 是否启用、最后同步时间和已同步的知识库目录，包括 owner
尚未解析的 PERSONAL 知识库。

## 企业主体

管理员通过以下接口维护映射：

- `POST /api/polarrag/users/{user_id}/principals`；
- `GET /api/polarrag/users/{user_id}/principals`；
- `PATCH /api/polarrag/users/{user_id}/principals/{principal_id}`；
- `DELETE /api/polarrag/users/{user_id}/principals/{principal_id}`。

允许的 provider 为 `feishu` 和 `sharepoint`，主体类型为 `user` 或 `group`。
一个身份域内的同一个外部 user 主体只能映射到一个 PAS 用户；外部 group 可以
分配给多个 PAS 用户。已停用或已过期映射不会进入 `acl_context`。

PAS Department 继续承载本地组成员关系。本地组与外部企业组相互独立，一期不建立
二者间的对应关系。远程身份适配器一期只定义接口，不调用飞书或 SharePoint
远端服务，也不执行批量导入。

## MCP Tool

仅用户可见的目录包含：

- `list_knowledge_resources(cursor?, limit?)`；
- `kb_search(query, knowledge_resource_ids, search_mode?, top_k?,
  min_score?, reranker?)`；
- `kb_fetch_context(knowledge_resource_id, doc_id, chunk_index,
  window_size?)`；
- `doc_find_by_name(knowledge_resource_ids, filename, limit?)`；
- `doc_status(knowledge_resource_id, doc_id)`；
- `doc_recall(knowledge_resource_id, doc_id, query, top_k?)`；
- `doc_get_original(knowledge_resource_id, doc_id)`；
- `doc_delete(knowledge_resource_id, doc_id)`；
- `doc_rechunk(knowledge_resource_id, doc_id, chunk_strategy?,
  chunk_max_tokens?)`。

用户专用 Agent Token 的目录还包含：

- `prepare_document_upload(knowledge_resource_id, filename, file_size_bytes,
  file_md5, file_sha256, content_type?)`；
- `resume_document_upload(upload_session_id)`；
- `complete_document_upload(upload_session_id, parts)`；
- `abort_document_upload(upload_session_id)`。

凡出现 `knowledge_resource_ids` 均为必填。一次请求中剩余的可访问资源必须属于同一
PolarRAG 实例和同一 Space。`kb_search` 可以并发查询多个知识库，按 score 合并
命中，并对单个不可访问知识资源或可重试的单 KB 上游失败返回脱敏
`partial_failures`。混用实例、混用 Space、参数非法、认证无效或身份上下文不可用
会导致整个请求失败。

当 `reranker=true` 但所选 Space 未配置 reranker 模型时，`kb_search` 返回
`RERANKER_NOT_CONFIGURED` 和脱敏说明。管理员完成 Space 配置前，该错误不可重试；
只有在业务允许不使用 reranker 时，客户端才可显式改为 `reranker=false` 重试。
PAS 不会暴露上游响应体、Space 标识、request id、账号、Endpoint 或凭证信息。

`doc_get_original` 调用 PolarRAG 受保护的文档信息接口。PolarRAG 完成 READ
鉴权后，PAS 只返回 `filename`、`file_type`、大小、内容哈希和 `oss_path` 等
文件元数据。PAS 不下载或代理文件、不生成签名 URL，也不处理 OSS 凭证。

`doc_delete` 是破坏性操作，只会在 PolarRAG 通过 `MANAGE` 鉴权后执行。
`doc_rechunk` 要求 PolarRAG `EXECUTE` 权限；`chunk_strategy` 可选 `hybrid`、
`hierarchical` 或 `inherit`。`inherit` 恢复当前 Space 默认策略，可能返回 `noop`。
两个 Tool 都会先验证文档属于所选知识资源，并且只使用服务端推导的 ACL 上下文。

上传 Tool 不接受本地路径、文件字节、OSS 坐标、凭证、身份或 ACL 字段。
`prepare_document_upload` 创建有效期 24 小时的上传会话，并返回 8 MiB 分片的
签名 URL；URL 在 15 分钟后失效。受支持的本地脚本把文件直接流式上传至 OSS，断点
信息只保存会话 ID、文件指纹以及已完成分片的 ETag。`resume_document_upload` 从
OSS 读取实际状态，只为缺失分片返回新的 URL。`complete_document_upload` 会先将
调用方报告的每个 ETag 和分片大小与 OSS 实际状态核对，再完成对象、重新构造当前
可信 actor 上下文并提交 PolarRAG；返回的 `doc_id` 只能由 PolarRAG 权威分配，PAS
不会生成。如果 OSS 已完成但 PolarRAG 提交失败，调用方有上限的重试只会再次提交
PolarRAG，不会重复完成 multipart。`abort_document_upload` 只能清理属于当前 PAS
用户和 Agent 的未完成会话。文件上限仍为 100 MiB。

## 所需 PolarRAG 能力

PAS 要求 PolarRAG 提供受保护的搜索、文档信息、按文件名查找、上下文读取和
文档内搜索接口。文档管理还会调用：

- `POST /_plugins/_polar_rag/spaces/{space_id}/managed_documents`；
- `DELETE /_plugins/_polar_rag/spaces/{space_id}/managed_documents/{doc_id}`；
- `PUT /_plugins/_polar_rag/spaces/{space_id}/documents/{doc_id}/chunk_strategy`。

PAS 同时探测两个固定的可信目录路由：

- `POST /_plugins/_polar_rag/spaces/_list`；
- `POST /_plugins/_polar_rag/spaces/{space_id}/knowledge_bases/_list`。

Space 目录必须返回不可修改且非空的 `identity_domain`，并为允许上传的 Space
返回非敏感的 `oss_bucket`。知识库目录必须返回 active KB 元数据，并为每个
`PERSONAL` KB 返回结构化 `owner`。两个目录均使用 opaque cursor 分页。

受保护的文档提交接口接收服务端推导的 Space/KB 坐标、OSS 对象元数据、全部可信
principals、user actor 和 `POLARRAG_DERIVED` ACL 标记。配置的共享 OpenSearch
账号不必是 `admin` 或 metastore 用户；PolarRAG 根据可信 actor 鉴权并从 active KB
生成最终 ACL。

不兼容的实例会被标记为 `capability_missing`；Space 枚举或启用返回
`OPERATION_NOT_SUPPORTED`。PAS 不接受手工提供目录事实作为降级方案。
