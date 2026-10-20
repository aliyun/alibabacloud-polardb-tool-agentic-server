# PolarRAG 知识库平台 HTTP API

[English](../../en/knowledge/polarrag-platform-http-api.md) | **简体中文**

本文面向客户知识库平台，说明 PAS HTTP 资源 API：知识库与文档 API、检索、HTTP Tool
网关，以及将企业用户绑定到知识资源的管理员 API。

示例使用 `https://pas.example.com`。在对应 PAS 版本中执行
`python -m server.openapi --surface application`，离线导出机器可读的请求和响应契约。
在线 `/openapi.json` 已禁用。

## 1. 范围与授权模型

### 管理员目录同步

`POST /api/v1/admin/spaces/{space_id}/sync` 同步已注册 Space 的目录。PAS 自动
查找所属实例，无需 `instance_id`。本接口及同路径 `GET` 状态接口要求启用状态的
PAS 管理员登录 access JWT（`Authorization: Bearer <login-access-token>`）
或 `session_token` Cookie。普通用户返回 403，未认证或凭据无效返回 401。
平台 OAuth、外部 Token 兑换、个人及 Agent Token 不能代替管理员登录凭据。
知识功能必须启用。

不传 body 或传 `{}` 执行原有全量同步；传 `{"kb_ids":["pkb-a","pkb-b"]}`
在 PolarRAG 高于 1.0.7 的版本上执行定向同步。允许 1–100 个字符串 ID，每个最长 256 字符，去除首尾空白并去重。
空数组、空白 ID、null 或类型错误返回 422。PAS 在每个上游
`knowledge_bases/_list` 分页请求中下推 `kb_ids`，要求 PolarRAG 支持该过滤参数；
不会退化为本地过滤全量目录，也不会以缺失完整元数据的单 KB 查询替代。
已登记插件版本为 1.0.7 及以下（或版本缺失、无法识别）时，PAS 忽略通过参数校验的
`kb_ids`，不向上游传该字段，直接执行全量同步。这包括缺失 KB 停用清理、Space 级
失败处理和全量结果计数；这些版本不能依赖定向隔离。支持普通语义版本及 PolarRAG 包版本字符串。

HTTP 202 和 `status: running` 只表示已接收。轮询同路径 `GET`，直到
`completed` 或 `failed`。这里返回 Space 最新状态，并非各请求的历史记录。
完成状态与目录更新在同一事务提交。未注册 Space 返回 404；Space 已停用或已有
同步租约返回 409。全量和定向同步共用同一个租约。
同一 Space ID 登记到多个实例时，两个新接口均返回 409 和 `AMBIGUOUS_SPACE`，
即使其中一条登记已停用也不例外。PAS 不会选择任一实例或启动同步；请使用原有
包含实例 ID 的接口操作目标登记。

支持定向同步时，要求所有目标都出现在上游 ACTIVE 目录中。目标缺失、非 ACTIVE 或属于
其他 Space 时，状态为 `failed`，错误为 `POLARRAG_CATALOG_TARGETS_MISSING`，
目标更新均不提交。上游返回非目标或格式错误时，错误为 `POLARRAG_INVALID_RESPONSE`；
其他上游异常同样失败。定向失败全部回滚，包括上游 HTTP 409；不修改非目标 KB、
Agent 绑定或用户授权。结果计数仅针对目标 KB，不执行缺失 KB 停用清理，
也不更新上次全量同步时间。同步登记成功不代表自动获得访问权。

原有 `POST` 和 `GET /api/polarrag/instances/{instance_id}/spaces/{space_id}/sync`
保持兼容，包括旧 POST 在已有同步运行时返回 202 的行为。

### 用户资源授权

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

个人 Token 使用 `/mcp/personal`，不作为本页 `/api/v1` 接口的凭据。个人连接仅可
访问当前账号有效权限范围内的数据库实例（包括直接授权、本人名下的系统分配实例及部门/用户组
继承权限），以及检索、读取本人拥有的 PERSONAL 知识库；PUBLIC
及他人的 PERSONAL 知识库不在其范围内。PUBLIC 知识库需通过已授权的 Agent 访问。

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
`knowledge_resource_id` 来自知识库列表，是 PAS 对外 ID。文档列表还支持从 1 开始的
`page`；`cursor` 与 `page` 二选一，不要同时传递。`doc_id` 来自上传、列表、详情或
检索响应。

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
| 向同一 Space 的多个知识库上传文档（最多 20 个文件） | `POST` | `/api/v1/spaces/{space_id}/documents/batch` |
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
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?limit=20&page=2
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents?filename=guide.pdf&limit=20
Authorization: Bearer <platform-user-token>
```

PolarRAG 1.0.7 及以上版本中，PAS 按 `updated_at` 倒序请求分页，并返回精确的
`total`、`page` 和 `size`。存在后续页时仍返回 opaque `next_cursor`，已有 cursor
客户端无需理解上游页码即可继续翻页。沿 cursor 翻页时必须保持相同 `limit`；PAS 会
拒绝不同值，避免分页边界重复或跳过文档。旧版 PolarRAG 保持文档 ID cursor 协议，
并省略 `total`、`page` 和 `size`。若翻页期间 PolarRAG 版本发生变化，PAS 会拒绝另一种
分页协议生成的 cursor；客户端需要从第一页重新开始。

当 PolarRAG 在文档列表或详情中返回 `source` 时，PAS 会原样透传该字段。该值由上游定义，
应按不透明字符串处理；当前观察到 `NATIVE`、`OSS`，但客户端不能假定只会出现这两种值。

PolarRAG 1.0.7 及以上版本中，文档列表、按文件名查找和文档详情响应还会包含 `acl`
对象。PAS 每批最多查询 200 个文档 ID，并且只返回经校验、属于当前调用者的 ACL 字段：

```json
{
  "acl": {
    "identity_domain": "tenant-a",
    "state": "ACTIVE",
    "updating": false,
    "acl_version": 3,
    "acl_set_id": "base64url-acl-set-id",
    "grants": [
      {
        "principal": {"provider": "feishu", "type": "group", "id": "engineering"},
        "actions": ["READ"]
      }
    ]
  }
}
```

具有 `MANAGE` 权限的调用者可能看到完整 grants；只读调用者只会看到自身生效的 grant，
具体裁剪由 PolarRAG 强制执行。`acl: null` 表示新版插件没有为该可读文档返回活动 ACL。
旧版插件省略 `acl`，PAS 也不会调用权限接口。

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

按文件名查找的每个 `items` 条目除已有的 `knowledge_space_id` 和嵌套 `document` 外，
还包含 `knowledge_resource_id`、`space_id`、`kb_id`。Space/KB 标识来自 PAS 资源目录，
不依赖上游文档是否携带这些字段。部分失败项遵循第 6 节的标识规则。

通过 `multipart/form-data` 上传一个文件：

知识库由 URL 选择；multipart 请求体只需提供 `file` 字段。

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

如需在一个请求中向同一 Space 的不同知识库上传最多 20 个文件，请按相同顺序重复
提交 `files` 和 `kb_ids` multipart 字段：

```bash
curl --fail-with-body \
  -H "Authorization: Bearer ${PAS_ACCESS_TOKEN}" \
  -F "files=@./policy.pdf" \
  -F "files=@./faq.md" \
  -F "kb_ids=kb-policy" \
  -F "kb_ids=kb-faq" \
  "${PAS_BASE_URL}/api/v1/spaces/${SPACE_ID}/documents/batch"
```

PAS 会在产生任何上传操作前校验全部文件和目标知识库。全部受理时返回 HTTP 201；
可明确归属到单个文件的 OSS 或 PolarRAG 失败只影响对应文件，并返回 HTTP 207，
`index` 表示原请求顺序：

```json
{
  "accepted": [{
    "index": 0,
    "knowledge_resource_id": "resource-policy",
    "kb_id": "kb-policy",
    "doc_id": "doc-001",
    "filename": "policy.pdf",
    "status": "DISPATCHED"
  }],
  "failed": [{
    "index": 1,
    "knowledge_resource_id": "resource-faq",
    "kb_id": "kb-faq",
    "filename": "faq.md",
    "status_code": 502,
    "code": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
    "message": "PolarRAG did not accept the uploaded document."
  }]
}
```

两个 multipart 列表长度必须一致。PAS 会在首次写 OSS 前检查鉴权、Space 归属、
知识范围、`EXTERNAL_SYNC` 写保护、OSS 就绪状态、文件名规则以及单文件 100 MiB 限制。
PAS 把对象上传到 OSS 后，通过一次 PolarRAG `managed_documents/_bulk` 请求提交全部
上传成功的对象。PAS 不发送 `doc_id`，由 PolarRAG 生成每个文档 ID。上传开始后不是
事务操作；客户端只应重试 `failed` 中的条目。
如果 PolarRAG 未返回明确的批量结果，PAS 返回 HTTP 502 和
`POLARRAG_BATCH_SUBMIT_RESULT_UNKNOWN`，并保留已上传的 OSS 对象。此时 PolarRAG
可能已经受理请求，客户端不得盲目重试。

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

检索响应保留 `doc_generation`。为保证上下文窗口与所选检索命中一致，
将命中的 generation 传入可选查询参数 `expected_doc_generation`
（0 到 2147483647 的整数）：

```http
GET /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/chunks/{chunk_index}/context?window_size=2&expected_doc_generation=2
```

两种上下文路由还接受可选的 URL 编码 `query`，其值为原查询。
遇到 HTTP 409 `citation_source_changed` 时，PAS 使用该查询在同一文档和 KB 内
最多重新定位一次片段，再按新 Chunk 的 index 和 generation pin 读取上下文。
没有新命中、缺少 generation 或再次冲突时，返回 HTTP 409，
`detail.code=citation_source_changed`。已发布 generation 不可见、缺失或目标 Chunk
缺失也会产生该冲突。应丢弃旧命中及已组装上下文，禁止通过移除 pin 重试。
省略 `query` 时，调用方须使用原查询和原选择条件最多重跑一次原检索，再读取新命中
的固定 generation 上下文。如果带 `query` 的自动恢复已失败，应停止，不能再启动
重试循环。该恢复不重跑跨 KB 检索；ACL 错误保留原有语义。
既有未传 pin 的请求仍然受支持，但不保证检索到上下文之间的一致性。

Chunk 列表和上下文响应中的每个 Chunk 条目都会返回已授权目标文档的原始
`filename`。PAS 从已通过 ACL 校验的文档记录中取得该字段，不接受客户端传入的文件名，
也不会为每个 Chunk 单独查询。中文、空格和多后缀文件名保持原值。
上游 Chunk 的 `source_uri` 若存在则原样透传：

```json
{
  "items": [{
    "doc_id": "doc-001",
    "filename": "退款 指南.backup.v2.pdf",
    "source_uri": "oss://bucket/refund.pdf",
    "chunk_index": 3,
    "text": "原始 Chunk"
  }],
  "offset": 0,
  "limit": 100,
  "total": 1,
  "next_offset": null
}
```

### 强制重新切分

单文档和批量重新切分均可提交可选的 `force`（默认 `false`）。`force=true` 仅适用于
PolarRAG 1.0.7 及以上版本；目标版本较低或未知时，单文档接口返回 HTTP 422，错误码为
`POLARRAG_VERSION_UNSUPPORTED`。批量接口将此类失败记录在 `failed` 中，条目的
`status_code` 为 422，整体返回 HTTP 207（包括全部文档失败时）。
强制重新切分仍执行原有用户、Agent、KB 和文档授权检查。

```http
POST /api/v1/knowledge-bases/{knowledge_resource_id}/documents/{doc_id}/rechunk
Authorization: Bearer <platform-user-token>
Content-Type: application/json

{"chunk_strategy":"hybrid","chunk_max_tokens":512,"force":true}
```

按 Space 和 KB ID 定位时，改用
`POST /api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{doc_id}/rechunk`，
请求体相同。

### 批量重新切分

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
范围为 1 到 1000。响应包含 `items`、`offset`、`limit`、`total` 及 `next_offset`；
只要 `next_offset` 不为 `null` 就继续请求。PolarRAG 返回精确命中数
（`relation=eq`）时，PAS 将其作为 `total` 返回并据此计算 `next_offset`；否则
`total` 为 `null`，PAS 仅在一页已满时额外探测下一条 Chunk。每个条目都是受 ACL
保护的 Chunk source，且始终包含 `image_resources`（上游 Chunk 没有图片时为空数组）。

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
不会静默截断。检索多个 KB 时，PAS 探测运行时 `_search_capabilities`，按
`max_kb_ids` 分批发送 `kb_ids`，不会因登记的插件版本较旧而忽略已声明的能力。
能力接口返回 400、404 或 501，或明确返回 `multi_kb_search: false` 时，仍逐个发送旧 `kb_id`。
上游检索返回 HTTP 429 时按暂时不可用处理：保留成功 KB 的结果，受影响的 KB
通过 `partial_failures` 报告 `POLARRAG_UNAVAILABLE`；全部 KB 均失败时返回 HTTP 503。

所有知识库检索和单文档检索响应都会在每个 `items` 条目中返回 ACL 已授权上游命中的
原始 `filename` 与上游 `source_uri`；上游缺失或为 null 时，检索响应中的
`source_uri` 为 `null`。`POST /api/v1/knowledge-bases/search` 和
`POST /api/v1/spaces/{space_id}/search` 的响应还会在保留
`knowledge_resource_id` 的同时返回 `space_id`、`kb_id`。每组标识均对应实际资源，
即使结果来自不同 Space 下同名的 KB ID，也不会混淆。
`partial_failures` 中可访问资源也包含这两个字段；错误为
`KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE` 时，两者均为 `null`，避免暴露资源标识。
资源已不在 PAS 目录中时，两个字段也为 `null`。

这些都是新增的 HTTP 响应字段，已有 ID、内容、状态字段、分页、排序、错误和请求选择器
语义不变。客户端应忽略未知响应字段。原生 MCP Tool 结果及 `/api/v1/tools/call`
会从既有已授权检索命中或文档详情中返回同一个文件名；检索工具也返回上游 `source_uri`。

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
    "filename": "退款 指南.backup.v2.pdf",
    "source_uri": "oss://bucket/refund.pdf",
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

管理员可通过 `GET /api/agents/{agent_id}/all-subject-assignments` 和同路径 `POST`
管理 Agent 的全部成员。POST 接受 `{"operation":"BIND"}` 或 `{"operation":"UNBIND"}`。
BIND 添加已绑定实例的启用 Space 下全部可用身份源，以及全部启用的自建用户，不受分页影响。
整个身份源授权包含后续同步用户；新建自建用户或身份源后需再次 BIND。
UNBIND 移除当前 Agent 的全部用户、部门、组和身份源成员关系，并使对应用户的 Agent Token 失效。
依赖组成员绑定的 KB 授权随之删除；KB 用户策略、其他 Agent 和数据库实例授权保留。
重复操作幂等。GET 和 POST 均返回 `status`（`BOUND`、`PARTIAL`、`UNBOUND`）、
`eligible_source_count`、`eligible_user_count` 和 `assignment_count`。
Agent 不存在返回 `404`，非管理员返回 `403`，未认证返回 `401`，参数非法返回 `422`，
旧版批量绑定任务仍运行时返回 `409`。该接口管理 Agent 成员，不是 KB 级全部用户策略。

Agent 到 KB 的可见关系通过 `GET /api/admin/agents/{agent_id}/knowledge-resource-options`
查询，支持可选 `binding_status=bound|unbound`、`management_mode`、`search`、`offset`
和 `limit`（1–100）。筛选发生在分页及响应 `total` 统计之前。
`POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` 和 `:unbind` 接收
`{"knowledge_resource_ids":["<resource-id>"]}` 或
`{"targets":[{"space_id":"<space-id>","kb_id":"<kb-id>"}]}`（1–100 项，
两个选择字段必须且只能提交一个），原子、幂等地绑定或解绑
自建 PUBLIC/PERSONAL KB。目标必须活跃、启用并位于已绑定实例及符合父级约束的 Space 下；
无效批次返回 `422`。两个接口均返回 `instance_count`、各类型 `statuses`、
`agent_id` 和本次接受的排序后 `knowledge_resource_ids`。
保留用户策略及 Agent 成员关系，PERSONAL 仍仅 owner 可见。从全部范围部分解绑会保留
动态全选并记录 KB 排除项，后续新增自建及同步 KB 仍在可见范围内。重新绑定会移除所选
排除项，绑定全部或解绑全部会清空对应类型的排除项。迁移保留旧的显式允许列表。
控制台分已绑定/未绑定页并支持多选。

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
## Space/KB 成对选择兼容

内部资源 ID 方式继续支持。以下等价形式需要同时提供 `space_id` 和 `kb_id`，不能只提供孤立的 `kb_id`：

| 操作 | 成对选择形式 |
| --- | --- |
| `POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` 和 `:unbind` | 用 `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` 代替 `knowledge_resource_ids` |
| `PUT /api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources` | 用 `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` 代替 `public_knowledge_resource_ids` |
| `POST /api/admin/agents/{agent_id}/knowledge-binding-status`、`/knowledge-subject-options`、`/knowledge-all-users-status`、`/knowledge-all-users` | 用 `{"targets":[{"space_id":"space-1","kb_id":"kb-1"}]}` 代替 `knowledge_resource_ids`；保留各接口的其他字段 |
| `PUT /api/polarrag/knowledge-resources/{knowledge_resource_id}/management-mode` | 改用 `PUT /api/polarrag/spaces/{space_id}/knowledge-bases/{kb_id}/management-mode`，请求体不变 |
| `/api/me/polarrag/documents/_find`、`/_list`、`/{doc_id}`（DELETE）、`/{doc_id}/rechunk` | JSON 用 `space_id` 和 `kb_id` 代替 `knowledge_resource_id`，保留 `agent_id` 及其他操作字段 |
| `POST /api/me/polarrag/documents` | 不传 `knowledge_resource_id` 时使用 multipart 表单 `space_id` 和 `kb_id`，保留 `agent_id` 和 `file` |
| `POST /api/me/polarrag/documents/batch` | multipart 表单用 `space_id` 和重复的 `kb_ids` 代替重复的 `knowledge_resource_ids`；每个 `kb_ids` 与对应的 `files` 同序匹配 |

PUBLIC 范围 PUT 仍为整表替换：`targets: []` 清空，`targets: null` 恢复动态 PUBLIC 全选。`targets` 与 `public_knowledge_resource_ids` 必须且只能出现一个。校验和审计复用原替换操作，与增量 `knowledge-visibility:bind` 不同。

上述四个管理员请求体须从 `targets` 和 `knowledge_resource_ids` 中恰好选择一个非空列表（1–100 项）。文档批量上传须选择 `knowledge_resource_ids`，或 `space_id` 加 `kb_ids`，不得混用；上传前逐文件校验用户的 Agent 与 KB 权限。

文档 JSON 和 multipart 的两种选择方式均互斥：内部 ID 与任意成对字段混用时，在上传前返回 422。成对解析不改变 Agent/用户授权、资源管理规则或文档 ACL。目标不存在返回 404，在可选实例中存在歧义返回 409。管理模式别名复用内部 ID 路由的管理员校验、更新和必需审计。

现有知识库批量绑定、外部身份源绑定快照、`/api/v1/documents/_find` 和 `/api/v1/knowledge-bases/search` 已接受包含 `space_id`、`kb_id` 的 `targets`。现有 `/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}` 文档别名不变。Space 检索在 Space 路径下接受 `kb_ids`；省略全部选择参数时使用调用者在 Agent 下的可见范围。


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
