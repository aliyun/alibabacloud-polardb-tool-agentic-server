# PolarRAG 知识库平台 HTTP API

[English](../../en/knowledge/polarrag-platform-http-api.md) | **简体中文**

本文面向客户知识库平台，说明 PAS HTTP 资源 API：知识库与文档 API、检索、HTTP Tool
网关，以及将企业用户绑定到知识资源的管理员 API。

示例使用 `https://pas.example.com`。目标 PAS 的 `/openapi.json` 是机器可读的请求和
响应契约。

## 1. 范围与授权模型

每个资源请求均要求 PAS 用户 Access Token，并具有如下有效授权上下文：

- `aud` 为 `<PAS base URL>/api/v1`；
- `scope` 包含 `polarrag`；
- Token 代表 PAS 用户和有效 Agent 上下文；
- PAS 在收到每个请求时重新确认该用户仍可访问该 Agent。

外部应用可通过 `/token` 或 `/api/v1/external-auth/token` 的 RFC 8693 Token
Exchange 获取此 Token。管理员在**外部应用**页面注册调用方服务，PAS 会集中展示
`client_id`、只显示一次的 `client_secret`、Resource
`<PAS base URL>/api/v1`、Scope `polarrag`、Agent 策略和可复制请求。交换只返回
PAS Access Token，不返回 Refresh Token。除非单独启用直连兼容模式，PAS 不会把
飞书 `user_access_token` 直接当作资源 API Bearer Token。

Admin Token 不能替代用户 Token 访问知识内容。现有 Dashboard Session、PAS MCP OAuth、
`pas_agent_` Token 和 `pas_user_agent_` Token 在各自接口中继续支持。

最终权限始终取以下条件的交集：有效用户、有效 Agent、当前 User-to-Agent 授权、
Agent 资源范围、身份源绑定和 PolarRAG 文档 ACL。客户端提交的 principal 或 ACL
上下文不能覆盖服务端状态。

本版本不创建或删除上游知识库，不接收客户端定义的文档 ACL，也不生成 LLM 答案。

## 2. 接入前准备

管理员需要先完成：

1. 通过 HTTPS 发布 PAS；
2. 配置并同步一个 ACTIVE 的飞书或 SharePoint 身份源；
3. 注册 PolarRAG，并把身份源绑定到目标 Space；
4. 创建 Agent，为其授权 PolarRAG 资源以及用户或用户组；
5. 使用 Token Exchange 时，配置外部 Token 信任并注册外部应用；
6. 需要上传文档时，验证 Space 的 OSS 配置。

## 3. 通用 HTTP 契约

知识接口使用第 1 节所述的 PAS 用户 Access Token：

```http
Authorization: Bearer <pas-user-access-token>
Accept: application/json
```

列表接口使用 opaque `cursor`。客户端原样传递 `next_cursor`；`null` 表示最后一页。
`knowledge_resource_id` 来自知识库列表，是 PAS 对外 ID；`doc_id` 来自上传、列表、
详情或检索响应。

当前支持的知识与 Tool 接口如下：

| 能力 | 方法 | 路径 |
| --- | --- | --- |
| 枚举知识库 | `GET` | `/api/v1/knowledge-bases` |
| 获取知识库 | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}` |
| 获取 Space | `GET` | `/api/v1/spaces/{space_id}` |
| 枚举 Space 下的知识库 | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases` |
| 按 Space 和 KB ID 获取知识库 | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}` |
| 枚举文档 | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents` |
| 上传文档 | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents` |
| 获取或删除文档 | `GET`、`DELETE` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}` |
| 分页获取文档 Chunk | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks` |
| 获取 Chunk 上下文窗口 | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context` |
| 获取原文件元数据 | `GET` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/original` |
| 请求文档重新切分 | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk` |
| 批量请求文档重新切分（最多 20 个文档） | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk` |
| 跨指定知识库按文件名查找文档 | `POST` | `/api/v1/documents/_find` |
| 准备直传 OSS 的文档上传 | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/document-uploads` |
| 完成直传 OSS 的文档上传 | `POST` | `/api/v1/document-uploads/{upload_session_id}/complete` |
| 按 Space 和 KB ID 枚举或上传文档 | `GET`、`POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents` |
| 按 Space 和 KB ID 获取或删除文档 | `GET`、`DELETE` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}` |
| 按 Space 和 KB ID 分页获取文档 Chunk | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks` |
| 按 Space 和 KB ID 获取 Chunk 上下文窗口 | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks/{chunk_index}/context` |
| 按 Space 和 KB ID 获取原文件元数据 | `GET` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/original` |
| 按 Space 和 KB ID 请求重新切分 | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/rechunk` |
| 按 Space 和 KB ID 批量请求重新切分（最多 20 个文档） | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk` |
| 按 Space 和 KB ID 准备直传 OSS 上传 | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/document-uploads` |
| 检索单个知识库 | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/search` |
| 按 Space 和 KB ID 检索单个知识库 | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/search` |
| 检索多个知识库 | `POST` | `/api/v1/knowledge-bases/search` |
| 检索同一 Space 下的多个知识库 | `POST` | `/api/v1/spaces/{space_id}/search` |
| 检索单个文档 | `POST` | `/api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/search` |
| 按 Space 和 KB ID 检索单个文档 | `POST` | `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/search` |
| 枚举 Tool | `GET` | `/api/v1/tools` |
| 调用 Tool | `POST` | `/api/v1/tools/call` |

## 4. 知识库与文档

枚举当前用户可见知识库：

```http
GET /api/v1/knowledge-bases?limit=50&cursor=<optional-cursor>
Authorization: Bearer <platform-user-token>
```

每项包含 `knowledge_resource_id`、上游 `space_id`、本地 `knowledge_space_id`、展示名、
上游 `kb_id`、`kb_type`、`usage` 和 `upload_ready`。当 PAS 配置的实例中恰好解析到一个
同名 `space_id` 时，`space_id + kb_id` 可定位一个知识库；同一 `space_id` 下可有多个
`kb_id`。知识库可见不代表其中所有文档都可见，PolarRAG 仍会执行文档 ACL。

PAS 管理的资源使用 `management_mode=NATIVE`。管理员可通过
`PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode`
将外部同步任务维护的资源设为 `EXTERNAL_SYNC`。此类资源仍可检索，但 PAS 用户和 MCP
发起的上传、删除及重新分块会返回 `EXTERNAL_SYNC_RESOURCE_READ_ONLY`，列表中的
`upload_ready` 为 `false`。

可按 Space 枚举或获取一个知识库：

```http
GET /api/v1/spaces/{space_id}
GET /api/v1/spaces/{space_id}/knowledge-bases?limit=50&cursor=<optional-cursor>
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}
Authorization: Bearer <platform-user-token>
```

若 PAS 本地目录将一个 `space_id` 解析为多个 PolarRAG 实例记录，以上 Space 路由及
`space_id + kb_id` 选择器返回 HTTP 409 和 `SPACE_ID_AMBIGUOUS`，不会任意选择其中一条。

枚举或按名称查找可读文档：

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?limit=20&cursor=<optional-cursor>
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?filename=guide.pdf&limit=20
Authorization: Bearer <platform-user-token>
```

当 PolarRAG 在文档列表或详情中返回 `source` 时，PAS 会原样透传该字段。该值由上游定义，
应按不透明字符串处理；当前观察到 `NATIVE`、`OSS`，但客户端不能假定只会出现这两种值。

可使用任意一种知识库标识，在一个请求中跨多个知识库按文件名查找：

```http
POST /api/v1/documents/_find
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "filename": "guide.pdf",
  "limit": 20,
  "targets": [
    {"knowledge_resource_id": "resource-001"},
    {"space_id": "space-001", "kb_id": "kb-policy"}
  ]
}
```

通过 `multipart/form-data` 上传一个文件：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${PAS_ACCESS_TOKEN}" \
  -F "file=@./guide.pdf" \
  "${PAS_BASE_URL}/api/v1/knowledge-bases/${KNOWLEDGE_RESOURCE_ID}/documents"
```

```json
{"doc_id":"doc-001","filename":"guide.pdf","status":"DISPATCHED"}
```

PAS 根据 Token 和服务端企业身份状态推导可信上传 actor，客户端不能提交 ACL
principal。上传成功仅表示任务已受理，应轮询文档详情直到解析进入终态。

获取详情、原文件元数据或请求删除：

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks?offset=0&limit=100
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/original
DELETE /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}
Authorization: Bearer <platform-user-token>
```

可获取某个 Chunk 周围受 ACL 保护的上下文窗口，或请求异步重新切分：

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context?window_size=2
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"chunk_strategy":"hybrid","chunk_max_tokens":512}
```

`chunk_index` 从 0 开始，`window_size` 范围为 0 到 100。重新切分在 PolarRAG 接受请求后
返回 HTTP 202。使用不带 `chunk_max_tokens` 的 `inherit` 恢复 Space 策略；`hybrid` 和
`hierarchical` 可带 1 到 100000 的正整数 `chunk_max_tokens`。两种操作均有表中所列的
Space+KB 对等路由。

在同一知识库中批量重新切分时，`doc_ids` 必须包含 1 到 20 个不重复的文档 ID。PAS 按
提交顺序，通过与单文档接口相同的授权链路逐个提交：

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"doc_ids":["doc-a","doc-b"],"chunk_strategy":"hybrid","chunk_max_tokens":512}
```

所有文档均被受理时返回 HTTP 202。若部分文档不能受理，PAS 仍继续处理其它 ID，并返回
HTTP 207：

```json
{
  "accepted": [{"doc_id":"doc-a","status":"accepted"}],
  "failed": [{"doc_id":"doc-b","status_code":403,"code":"DOCUMENT_PERMISSION_DENIED","message":"..."}]
}
```

若按 Space 和 KB ID 定位知识库，请调用
`POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk`，请求体相同。

相同的文档操作也可使用 Space 和 KB ID：

```http
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents?limit=20&cursor=<optional-cursor>
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/chunks?offset=0&limit=100
GET /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/original
DELETE /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}
Authorization: Bearer <platform-user-token>
```

原文件接口只返回受 ACL 保护的元数据，不返回文件字节、OSS AccessKey 或签名下载
地址。删除要求 PolarRAG MANAGE 权限，受理后返回 HTTP 202。

大文件可使用直传 OSS 的 multipart 上传，避免通过 PAS 代理文件字节：

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/document-uploads
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "filename": "guide.pdf",
  "file_size_bytes": 1024,
  "file_md5": "<32 位大小写十六进制字符>",
  "file_sha256": "<64 位大小写十六进制字符>",
  "content_type": "application/pdf"
}
```

响应包含 `upload_session_id` 和短期 part URL。客户端直接上传各 part 后，调用
`POST /api/v1/document-uploads/{upload_session_id}/complete`。PAS 会校验服务端 multipart
状态，只有所有 part 都存在时才提交对象给 PolarRAG。准备上传接口也有表中所列的
Space+KB 版本。

## 5. 文档 Chunk

Chunk 列表按 `chunk_index` 升序使用 offset 分页。`offset` 从 0 开始，`limit`
范围为 1 到 1000。响应包含 `items`、`offset`、`limit`、`total`（当前为 `null`，
因为上游计数不可靠）及 `next_offset`；只要 `next_offset` 不为 `null` 就继续请求。
PAS 仅在一页已满时额外探测下一条 Chunk，因此 `next_offset` 不依赖上游计数。每个
条目都是受 ACL 保护的 Chunk source，且始终包含 `image_resources`（上游 Chunk 没有
图片时为空数组）。

## 6. 检索契约

检索单个知识库：

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"退款流程","search_mode":"balanced","top_k":10,"min_score":null,"reranker":false}
```

也可通过 Space 和 KB ID 定位知识库，检索请求体相同：

```http
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"退款流程","search_mode":"balanced","top_k":10,"min_score":null,"reranker":false}
```

也可在一个请求中混用多个 PAS 资源 ID 和 `space_id + kb_id`：

```http
POST /api/v1/knowledge-bases/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "query": "退款流程",
  "targets": [
    {"knowledge_resource_id": "resource-001"},
    {"space_id": "space-001", "kb_id": "kb-policy"}
  ],
  "top_k": 10
}
```

省略顶层 `/api/v1/knowledge-bases/search` 的 `targets` 时，会穷举检索 Agent 与用户
有效范围内的全部知识资源。显式目标可以跨 PolarRAG 实例和 Space；PAS 会分组调用、
合并结果，并将不可访问目标保留为 `partial_failures`。单次有效范围最多 1000 个资源，
不会静默截断。PolarRAG 1.0.6 及以下逐个发送旧 `kb_id`；1.0.7 及以上按运行时
`_search_capabilities.max_kb_ids` 分批发送 `kb_ids`。

同一 Space 的多个知识库可以使用更简短的路径；传入的 `knowledge_resource_ids` 必须
属于路径中的 `space_id`，否则返回 HTTP 422 和 `KNOWLEDGE_RESOURCE_OUTSIDE_SPACE`：

```http
POST /api/v1/spaces/{space_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{
  "query": "退款流程",
  "kb_ids": ["kb-policy", "kb-faq"],
  "knowledge_resource_ids": ["resource-001"],
  "top_k": 10
}
```

该 Space 路径同时省略 `kb_ids` 和 `knowledge_resource_ids` 时，检索此 Space 中全部
有效资源；显式传入空数组属于非法参数。

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"退款期限","top_k":10}
```

```http
POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/search
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"query":"退款期限","top_k":10}
```

```json
{
  "items": [{
    "knowledge_resource_id": "resource-001",
    "doc_id": "doc-001",
    "chunk_index": 3,
    "content": "检索到的原始 Chunk",
    "score": 0.87,
    "image_resources": [{"id":"document-0/pictures/1","oss_uri":"oss://bucket/picture-1.png"}],
    "metadata": {"page_numbers":[4],"headings":[],"captions":[]}
  }],
  "top_k": 10,
  "partial_failures": []
}
```

`content` 是检索到的原始 Chunk，不是 LLM 答案。结果按返回的相关性分数排序。首期
只暴露 `chunk_index`；上游响应没有独立的稳定 Chunk 标识，因此客户端不能依赖该
字段。ACL 过滤和 `min_score` 可能使 `items` 少于 `top_k`。

## 7. 通过 HTTP 调用 MCP Tool

无需 MCP Session 或 JSON-RPC 即可枚举 Tool 并调用：

```http
GET /api/v1/tools
Authorization: Bearer <platform-user-token>
```

目录是唯一准确信息源，目前包含 `list_knowledge_resources`、`kb_search`、
`kb_fetch_context`、`doc_list_chunks`、`doc_find_by_name`、`doc_status`、`doc_recall`、
`doc_get_original`、`doc_delete`、`doc_rechunk`、`prepare_document_upload` 和
`complete_document_upload`。

```http
POST /api/v1/tools/call
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"name":"kb_search","arguments":{"query":"退款","knowledge_resource_ids":["resource-001"],"top_k":10}}
```

响应保留 MCP `CallToolResult` 的 `content` 和 `isError` 等字段。调用方必须同时检查
HTTP 状态与 `isError`。HTTP 网关与 `/mcp` 复用同一 Tool 注册表、输入 Schema、
鉴权、限流、审计与执行代码。

## 8. 企业用户与知识资源绑定

企业访问配置使用管理员 API。通过 `GET /api/identity-sources`、
`GET /api/identity-sources/{identity_source_id}/directory` 和
`GET /api/identity-sources/spaces` 获取真实 ID。

推荐使用原子的 preview/apply 流程：

```http
POST /api/agents/{agent_id}/enterprise-access/preview
Authorization: Bearer <admin-token>
Content-Type: application/json

{"identity_source_id":"source-id","all_synced_users":false,"directory_group_ids":["group-row-id"],"pas_user_ids":["user-id"],"knowledge_space_ids":["space-id"]}
```

管理员确认后，将返回的 selection 和 `preview_hash` 发送到
`POST /api/agents/{agent_id}/enterprise-access/apply`。预览过期时必须重新展示，不能
自动重试。

Agent 的 `polarrag-bindings`、`public-resources`、`user-assignments` 和
`group-assignments` 仍可用于底层自动化。这些绑定只缩小 Agent 候选范围，不能替代
PolarRAG 文档 ACL。

## 9. 错误与安全

稳定网关错误在 `detail` 中返回 `code` 和 `message`，参数校验使用 FastAPI 标准错误
数组。常见错误包括 `AUTH_REQUIRED`、`INVALID_GRANT`、`AGENT_ACCESS_DENIED`、
`KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE`、`DOCUMENT_NOT_ACCESSIBLE`、
`IDENTITY_CONTEXT_UNAVAILABLE`、`POLARRAG_OSS_NOT_CONFIGURED`、
`POLARRAG_TOOL_LIMITED` 和 `POLARRAG_UNAVAILABLE`。

不要重试 400、403、404 或 422。429 至少等待 `Retry-After`；临时 502/503 可使用有
上限的退避。保留 `X-Request-ID` 供问题排查，但不要记录密码、Token、authorization
code、PKCE verifier、身份提供方 Token、OSS 凭证或企业文档内容。

生产环境使用 HTTPS，按用户会话隔离缓存，并以 PAS 在线授权为准，不要仅依赖本地
解码 JWT 的内容。用户、Agent 或授权关系删除后，即使 Token 尚未到期，后续访问也会
失效。
