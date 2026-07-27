# 配置模块参考

[English](../../en/reference/configuration-modules.md)

运行时配置以加密、带 revision 的模块文档存储在元数据库中。只有
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY` 保留为进程启动配置。

## 模块目录

- `token_security`：共享 JWT 密钥环和 Token 生命周期。
- `core_admin`：首个内置管理员，依赖 `token_security`。
- `agent_token_auth`：管理员签发 Agent Token 的能力。
- `user_sso`：可选 OIDC 人类登录，依赖 `token_security`。
- `aliyun_access`：AccessKey 或 AssumeRole 凭证、地域和
  `openapi_network`。
- `agentic_db_purchase`：PolarDB 购买规格，依赖 `aliyun_access`。
- `resource_pool`：VPC 位置和资源池策略，依赖
  `agentic_db_purchase`。
- `runtime_policy`：外部 URL、CORS、连接池和 Worker 策略。
- `sql_security`：限制、阻止操作、确认、限流和审计。
- `observability`：日志和审计保留行为。

可选模块可以保持 `SKIPPED`。停用依赖项前先停用所有有效下游模块。

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

## 密钥与导出

Secret 字段在根密钥下独立加密。省略已有 Secret 会保留原值，显式支持的清除
动作才会删除。Describe 和导出响应只包含已配置/脱敏标记。
