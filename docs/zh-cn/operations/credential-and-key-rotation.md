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

## 根加密密钥

`0.0.2` 不提供在线根密钥重新加密。不要在同一运行数据库上替换
`PAS_ENCRYPTION_KEY`；密钥变化会导致 fail-closed 解密错误。数据库恢复时
必须保留并恢复原密钥。

## 验证

轮换后验证所有副本就绪、相应的管理员或 Agent 认证、后端连接测试和 Audit
Logs。验证完成后再删除临时密钥文件和外部密钥管理系统中的旧值。
