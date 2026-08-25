# 管理员接入 PolarRAG

[English](../../en/knowledge/polarrag-onboarding.md)

本文指导管理员从已就绪的 PAS 开始，为一个用户交付可用的 PolarRAG MCP 连接。流程采用原生 `polarrag` 身份提供方，覆盖 `PUBLIC` 与 `PERSONAL` 知识库，并通过 OSS 启用托管文档上传。

API 与授权契约参见 [PolarRAG MCP 集成](polarrag-mcp.md)。完整 PAS 安装流程请使用仓库中的 `deploy-polardb-agentic-server` Skill。

## 交付结果与前置条件

完成后，用户可以使用 `pas_user_agent_` Token 连接 MCP 客户端，发现有权限的知识资源，检索或读取文档，并通过受支持的上传客户端上传本地文档。

本文使用 PAS `v0.0.8` 及后续版本提供的上传契约：MCP 目录只暴露
`prepare_document_upload` 和
`complete_document_upload`，完成操作只接受 `upload_session_id`。不要在 PAS
`v0.0.7` 上使用本文：该版本暴露旧版的 `prepare_document_upload`、
`resume_document_upload`、`complete_document_upload` 和
`abort_document_upload`，完成操作还要求传入 `parts`。应改用该 `v0.0.7`
checkout 中的 `docs/en/knowledge/polarrag-mcp.md`。执行本文前，必须使用包含
本文的不可变 PAS revision，并确认 MCP 目录符合当前契约。

| PAS revision | MCP 上传工具 | 完成参数 | 是否使用本文 |
| --- | --- | --- | --- |
| 旧版 `v0.0.7` | `prepare_document_upload`、`resume_document_upload`、`complete_document_upload`、`abort_document_upload` | `upload_session_id`、`parts` | 否。使用该 checkout 的 `docs/en/knowledge/polarrag-mcp.md`。 |
| `v0.0.8` 或包含本文的更高不可变 revision | `prepare_document_upload`、`complete_document_upload` | `upload_session_id` | 是。 |

需要准备：

- 已部署的 PAS 和控制台管理员权限。
- PAS 可访问的 PolarRAG/OpenSearch 地址，以及能够调用 PolarRAG 目录与文档 API 的账号。
- 已有知识库且可以同步的 PolarRAG Space。
- 已登记在 PolarRAG 可信目录中的 OSS Bucket，以及限定到目标 Bucket 和 Prefix 的凭据。
- 稳定的 `PAS_ENCRYPTION_KEY`，PAS 使用它加密保存上游与 OSS 凭据。

不要把密码、AccessKey Secret、Bootstrap Token 或 Agent Token 放入工单、聊天、Shell 历史或截图。管理员应在 PAS 控制台直接输入，用户应在目标客户端直接使用自己的 Token。

## 1. 部署并完成 PAS 初始化

使用 `deploy-polardb-agentic-server` Skill 部署 PAS。在目标机器上从受保护文件读取 Bootstrap Token，仅在初始化页面使用一次，然后创建管理员账号。

继续前必须请求 `/readyz`，并确认 HTTP 状态码为 200，且同时满足：

- `mode` 等于 `READY`。
- `config_status` 等于 `CURRENT`。

仅能打开控制台或看到容器运行并不足以继续。若 PAS 仍处于 `SETUP`，先完成初始化；若 `config_status` 不是 `CURRENT`，先完成 PAS 要求的配置或迁移，再注册 PolarRAG。

## 2. 注册 PolarRAG 并启用 Space

在 **管理 > 实例** 中选择 **注册实例 > PolarRAG**，填写：

- 显示名称。
- 分开填写的 Endpoint 协议、主机和端口。
- OpenSearch 账号和密码。
- 与 Endpoint 证书匹配的 TLS 校验设置。

不要把凭据嵌入 Endpoint URL。注册后确认实例为活动状态，打开实例的 **Spaces** 抽屉，启用目标 Space 并同步目录。

PAS 会验证所需的上游路由。如果实例或 Space 报告 `capability_missing`，应停止交付并把缺失的能力名称交给 PolarRAG 运维人员处理，不能通过放宽 PAS 授权绕过。[集成参考](polarrag-mcp.md)列出了目录、文档与上传所需的能力。

## 3. 创建 PAS 用户并选择企业身份

在 **管理 > 用户** 中创建内置用户，通过合规的密钥通道把初始密码交给用户。用户记录包含不可变的外部 ID；即使之后修改用户名，它仍是原生身份的权威标识。

打开该用户的 **企业身份**。应根据目标知识资源上游 ACL 中的主体选择 Provider，并为每条映射使用已启用 Space 返回的身份域：

- `polarrag`：用户没有需要匹配的飞书或 SharePoint 身份时，选择基础原生 Provider。它只接受 `user` 类型，Principal ID 固定为该用户不可变的 PAS 外部 ID，控制台会锁定该值。
- `feishu`：用户需要发现或读取飞书同步文档，或资源 ACL 包含飞书用户或用户组时选择。可选择 `user` 或 `group`，Principal ID 必须与该 ACL 中的飞书主体 ID 完全一致。
- `sharepoint`：用户需要发现或读取 SharePoint 同步文档，或资源 ACL 包含 SharePoint 用户或用户组时选择。可选择 `user` 或 `group`，Principal ID 必须与该 ACL 中的 SharePoint 主体 ID 完全一致。

当同一用户需要访问多个 Provider 保护的资源时，可以为同一个 PAS 用户添加多条有效映射。每条映射只建立身份域成员关系，不会自动授予 Space 内的全部知识库。选择外部 Provider 不会导入身份或文档；在飞书与 SharePoint 企业身份自动同步的后续版本发布前，这些外部映射必须由管理员手工维护。

## 4. 开放 PUBLIC 与 PERSONAL 知识库

同步后必须分别处理 PolarRAG 的两种访问模型：

- `PUBLIC`：活动资源只有在用户完成身份映射且 Agent 绑定允许时才可发现。Agent 可以允许全部公共资源、指定公共资源或不允许任何公共资源；该范围只能收窄上游权限。
- `PERSONAL`：在 Space 中打开尚未认领的个人资源，选择已映射的 PAS 用户，再执行 **分配所有者并激活**。只有所有者可以发现和使用该资源，管理员身份也不会绕过所有权。

不要把 `PERSONAL` 资源 ID 放入 Agent 的公共范围选择器。需要转移所有权时，应使用明确的管理员流程，并检查对应审计事件。

## 5. 配置 OSS 上传权限

打开已启用的 Space，选择 **配置 OSS**。Bucket 与 Endpoint 来自可信的 PolarRAG 目录，只读不可修改。输入 OSS AccessKey ID 和 Secret，并将权限最小化到页面显示的 Bucket 与配置的 Prefix。

PAS 会执行写入并删除测试对象的凭据探测；策略还必须允许该 Prefix 上上传所需的分片操作。探测成功表示此 Space 已可上传，不代表可以访问其他 Bucket 或 Prefix。PAS 使用 `PAS_ENCRYPTION_KEY` 加密凭据，且不会通过 API 返回 Secret。

若探测失败，应修复明确的 OSS Endpoint、Bucket、Prefix 或权限策略问题。不要禁用探测，也不要用任意目标替换可信目录值。

## 6. 创建 Agent 并分配用户

在 **管理 > Agents** 中创建 Agent。其机器 Token 以 `pas_agent_` 开头；如其他 PAS 自动化需要可妥善保存，但不能用于 PolarRAG，因为机器 Token 不会获得 PolarRAG 工具。

打开 Agent 的 PolarRAG 设置并执行：

1. 绑定已注册的 PolarRAG 实例。
2. 设置允许的 `PUBLIC` 范围：全部、指定资源或无。
3. 分配前面创建的 PAS 用户。

用户分配、原生 Principal、活动 Space、资源 ACL 与 Agent 绑定必须同时匹配。Agent 绑定不能扩大 PolarRAG 授权，也不能把 `PERSONAL` 资源授予其他用户。

## 7. 派发并连接用户 Agent Token

让用户自行登录，在 **我的实例 > MCP 连接** 中选择已分配的 Agent，派发用户 Agent Token，并可选设置过期时间。Token 以 `pas_user_agent_` 开头。内置用户确认密码后可以 Reveal 现有 Token；SSO 用户新签发或重新生成的明文只返回一次。管理员只能查看状态或强制吊销，不能读取明文。

把 PAS 生成的 MCP 配置复制到目标客户端。典型 HTTP 连接如下：

```json
{
  "mcpServers": {
    "pas-polarrag": {
      "type": "http",
      "url": "https://pas.example.com/mcp",
      "headers": {
        "Authorization": "Bearer pas_user_agent_REDACTED"
      }
    }
  }
}
```

应使用 PAS 实际生成的 URL，反向代理可能带有不同路径前缀。Token 一旦泄露应立即吊销并重新派发，不要尝试修改原 Token。

## 8. 通过 Agent 上传文档

上传工具只负责协调可恢复传输，不会读取本地路径，也不会暴露 OSS 凭据。

1. 调用 `list_knowledge_resources`，选择目标不透明 `knowledge_resource_id`。
2. 受支持的本地上传客户端计算文件元数据，并通过 Agent 调用 `prepare_document_upload`。
3. 客户端把每个分片直接上传到返回的短期签名 URL，不要在这些请求中携带 PAS Token 或 OSS 凭据。
4. 调用 `complete_document_upload`。若返回缺失分片，只使用刷新后的 URL 上传缺失部分，再次完成。
5. 保存完成阶段返回的权威 `doc_id`，并轮询 `doc_status`，直到摄取成功或返回可处理的错误。

PAS 托管上传上限为 `100 MiB`；会话有效期为 `24` 小时，常规分片大小为 `8 MiB`，分片签名 URL 有效期为 `15` 分钟。如果客户端不支持签名分片 PUT，请改用 **我的实例** 下的 PAS Web 上传流程，不要要求 MCP 工具打开本地文件。

超过该 PAS 限额的文件，请使用企业知识空间自动文档上传：将文件上传到知识库对应的 OSS 路径，由服务自动同步并摄取。具体操作参见[使用企业知识空间：通过 OSS 上传](https://help.aliyun.com/zh/polardb/polardb-for-mysql/create-and-use-an-enterprise-knowledge-space#upload-oss-section)。这是企业知识空间的上传路径，不是要求 MCP 工具读取本地文件。

## 可用的 MCP 工具

正确授权的 `pas_user_agent_` 连接可以提供以下 PolarRAG 工具：

- `list_knowledge_resources`：列出调用者有权访问的不透明资源句柄。
- `kb_search`：在单个资源中检索，并返回排序后的文档摘要。
- `kb_fetch_context`：根据选定检索结果获取可溯源上下文。
- `doc_find_by_name`：按名称查找文档，不暴露上游知识库 ID。
- `doc_status`：查看摄取或处理状态。
- `doc_recall`：召回文档已索引的 Chunk。
- `doc_get_original`：在上游支持时获取原始文档。
- `doc_delete`：删除文档；仅在用户明确确认破坏性操作后使用。
- `doc_rechunk`：使用受支持的切分参数请求重新处理。
- `prepare_document_upload`：创建或恢复签名分片上传会话。
- `complete_document_upload`：校验已上传分片并提交文档摄取。

工具是否可见取决于上游能力与授权。缺少管理或上传工具通常意味着上游能力、资源 ACL、Agent 用户分配或 OSS 就绪状态不满足，不应改用机器 Token 绕过。

## 验收检查与故障排查

只有全部通过才算交付完成：

- `/readyz` 报告 `READY` 与 `CURRENT`。
- PolarRAG 实例为活动状态，Space 已启用并同步，且没有 `capability_missing`。
- 用户在正确身份域中存在活动的原生映射。
- `list_knowledge_resources` 返回预期的 `PUBLIC` 资源，并且只返回该用户已认领的 `PERSONAL` 资源。
- MCP 工具目录包含用例所需工具，端到端检索或上传成功。
- **审计日志** 包含本次操作预期的实例注册、身份、所有权、Agent 分配和上传事件。

常见问题：

- 没有资源：依次检查身份域、Principal、已启用 Space、资源状态、Agent 绑定、用户分配和公共范围。
- `capability_missing`：升级或配置 PolarRAG 以提供指定路由，PAS 无法模拟该能力。
- 上传不可用：检查可信目录目标与 OSS 凭据探测。
- 个人资源无权限：检查所有者；管理员权限不是 ACL 绕过手段。
- `RERANKER_NOT_CONFIGURED`：配置 PolarRAG Reranker，或使用上游支持的检索模式，不能用重试隐藏错误。

## 企业身份同步发布状态

当前版本支持人工维护 `feishu` 与 `sharepoint` 的用户或用户组 Principal 映射，但不会调用这两个提供方进行身份同步或批量导入。飞书与 SharePoint 企业身份自动同步将在后续版本中尽快发布，能力可用后会同步补充文档。当前不要把人工映射描述或配置成自动同步。
