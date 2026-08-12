# 凭证与密钥轮换

[English](../../en/operations/credential-and-key-rotation.md)

应根据所有者和生命周期轮换凭证。数据库凭证、Agent Token、云 AccessKey、
OIDC Secret 和 PAS 根密钥的流程不同。

## 数据库与 Agent 凭证

MySQL 密码变化后，编辑已有实例凭证，输入新密码并在保存前执行
**Test Connection**。不要重新创建物理实例。复查引用已吊销凭证的绑定。

重新生成 Agent Token 会立即使旧值失效。更新客户端密钥并重新连接。客户端
退役时使用吊销。

## 云与 SSO 密钥

编辑 `aliyun_access` 或 `user_sso`，从后端运行 dry run、验证并激活。VPC
模式应同时验证 STS 和 PolarDB 端点。仅在外部提供方要求的受控时间内保持旧
凭证有效，PAS 不提供双密钥重叠机制。

## Aliyun Access 模式变更

切换 `aliyun_access` 模式时，PAS 仅会在管理员保存变更后停止使用前一模式。
推荐的默认选择是 **Clear the previous credential**，它会移除旧模式块。
**Retain it, but keep it disabled** 会加密保留该块但使其处于非活动状态；它不会被
加载、验证或作为 fallback 选择。

要回到保留模式，必须显式选择 **Use retained credential**，再运行 dry run 并按
常规 confirmation 流程保存。也可以输入新值替换凭证。将保留的 direct AccessKey
复用为 AssumeRole 源凭证是独立、需确认的操作，绝不会自动复制。恢复不再需要时，
选择 **Delete retained credential** 删除非活动的保留块。

控制台仅通过简短、服务端生成的 `display mask` 显示 AccessKey ID。它绝不会返回
AccessKey Secret、External ID、STS 临时凭证或 security token。应把新的 Secret 输入
视为轮换，并只通过已配置的安全输入或 CLI 密钥引用提供。

## 根加密密钥

当前版本不提供在线根密钥重新加密。不要在同一运行数据库上替换
`PAS_ENCRYPTION_KEY`；密钥变化会导致 fail-closed 解密错误。数据库恢复时
必须保留并恢复原密钥。

## 验证

轮换后验证所有副本就绪、相应的管理员或 Agent 认证、后端连接测试和 Audit
Logs。验证完成后再删除临时密钥文件和外部密钥管理系统中的旧值。
