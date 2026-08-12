# 故障排查

[English](../../en/operations/troubleshooting.md)

从最小失败边界开始，并保留脱敏证据。

## 服务无法启动

运行 `pas database check`。重启前解决
`DATABASE_SCHEMA_NOT_INITIALIZED`、`DATABASE_SCHEMA_OUTDATED`、
`DATABASE_SCHEMA_TOO_NEW`、`DATABASE_MIGRATION_HEAD_INVALID` 或
`DATABASE_UNAVAILABLE`。不要绕过门禁，也不要让每个副本都执行迁移。

解密失败时，确认所有 Pod 使用同一个原始 `PAS_ENCRYPTION_KEY`。不要在生产
数据库上试用替换密钥。

## Pod 未就绪

检查 `/readyz`，比较 `desired_config_version` 和
`loaded_config_version`，再查看 `last_reload_error` 和模块错误。验证数据库
延迟，并确认已经经过轮询间隔。配置落后的 Pod 不接收 Service 流量是正确行为。

## 外部验证失败

DNS、路由、TLS、凭证和权限失败使用不同脱敏代码。VPC 模式下，从后端 Pod
测试地域 `polardb-vpc` 和 `sts-vpc` 端点解析。实例 Test Connection 也从该
Pod 发起；检查 MySQL 白名单、安全组、host、port、用户名和密码。

## 阿里云凭证错误

记录结果中显示的安全阿里云 Request ID，然后只修复命名的失败边界，不要把凭证或
原始 SDK 消息复制到工单。以下稳定代码适用于 `aliyun_access` 的验证和运行时失败：

| Code | 处理措施 |
| --- | --- |
| `OPENAPI_STS_SOURCE_CREDENTIAL_INVALID` | 通过安全输入替换 PAS 保存的 AssumeRole 源 AccessKey；确认它已启用且属于预期源身份。 |
| `OPENAPI_STS_ASSUME_ROLE_DENIED` | 只在已配置的目标 Role ARN 上向源身份授予 `sts:AssumeRole`。 |
| `OPENAPI_STS_ROLE_TRUST_REJECTED` | 修正目标角色 trust policy，使其允许预期源 principal，而不是扩大 source policy。 |
| `OPENAPI_STS_EXTERNAL_ID_MISMATCH` | 使已配置的 External ID 与目标 trust policy 要求的 condition 完全一致；不要记录或展示其值。 |
| `OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED` | 将预期 RAM 角色绑定到运行受影响 PAS Pod 的 ECS 实例，然后验证每个副本。 |
| `OPENAPI_ECS_METADATA_DISABLED` | 启用部署策略所需的实例 metadata service 访问，并从后端 Pod 重试。 |
| `OPENAPI_ECS_IMDSV2_UNAVAILABLE` | 恢复 Pod 到 IMDSv2 的可达性；PAS 没有 IMDSv1 fallback、metadata URL 覆盖或 HTTP proxy 路径。 |
| `OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED` | 修复 STS 或 IMDS 连通性及活动角色配置，再重试失败的云操作；PAS 会在过期后 fail closed。 |
| `OPENAPI_CREDENTIAL_INVALID` | 通过安全配置流程替换活动的直接凭证。 |
| `OPENAPI_PERMISSION_DENIED` | 只向活动目标角色或直接身份授予必需的 PolarDB OpenAPI 操作。 |
| `OPENAPI_DNS_FAILURE`、`OPENAPI_TLS_FAILURE`、`OPENAPI_CONNECT_FAILURE` | 从 PAS Pod 检查 DNS、路由、HTTPS `443`、证书信任及选择的 `public` 或 `vpc` 端点。 |
| `OPENAPI_ENDPOINT_UNSUPPORTED` | 修正所选地域或网络值；不支持自定义端点主机名。 |

不要把保留凭证、其他模式或 IMDSv1 作为自动变通方案。确认失败边界后，再执行显式、
可审计的配置变更。

## MCP 或 SQL 失败

绑定变化后重新连接。调用 `list_db_instances`，把返回的 `db_instance_id`
作为 `instance_id`，并确认绑定开放了所需 SQL 能力。然后验证已存 MySQL
账号具有请求数据库和语句权限。确定拒绝层之前不要扩大权限。

## 供应卡住

检查后端健康、容量、生命周期状态、Worker 所有权和资源失败代码。
`enable_multi_tenant` 必须开启，供应管理员必须通过预检。只通过受支持的恢复
动作重试，以保持幂等和清理状态完整。
