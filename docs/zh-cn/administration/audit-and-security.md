# 审计与安全

[English](../../en/administration/audit-and-security.md)

PAS 记录与安全相关的管理、认证、SQL、绑定、凭证和供应事件。审计记录用于
调查，但不能替代数据库原生审计或云控制面日志。

## 审计范围

检查操作者、动作、目标、结果、时间、请求上下文和脱敏失败类别。SQL 策略会
记录被阻止或确认的操作，但不会公开凭证。配置审计记录变化字段名、revision
和状态，同时排除密钥值。

控制台将同一条审计流分为 **All**、**SQL** 和 **PolarRAG** 三个视图。
PolarRAG Tool 记录会解析实例、Space、KB、命中数和部分失败元数据。PolarRAG
仍是文档 ACL 的最终权威；PAS 不记录可信 ACL 上下文或搜索文本。

## 密钥边界

数据库密码、Agent Token、bootstrap token、根密钥、AccessKey、OIDC
Secret、密文和 SQL 参数不得出现在普通日志或公开错误消息中。凭证显示和轮换
是显式、可审计的管理员流程。

## 运行时依赖基线

使用仓库的 `uv.lock` 部署。运行时要求 PyJWT 2.15.0 及以上、AnyIO 4.14.2
及以上、urllib3 2.8.0 及以上、certifi 2026.10.19 及以上、aiohttp 3.14.5
及以上，以及 multidict 6.9.1 及以上。使用自定义依赖约束安装 PAS 时，保留
这些最低版本要求。aiohttp 还要求在其支持的版本范围内使用 yarl 1.25.1 及以上。

畸形 JWT header 和 payload 会作为认证失败拒绝。身份提供方、云 SDK 及其他出站
HTTPS 连接应保持 TLS 证书和主机名校验启用。

certifi 证书包已移除受影响的 `emSign ECC Root CA - G3` 信任根，并保留未受
影响的根证书。自定义镜像应基于更新后的 lockfile 重新构建，并重启现有进程以
加载新的信任库；仅修改 lockfile 不会更新运行中的容器。如果使用自定义 CA
证书包或系统信任库，应单独检查并移除受影响的信任根。保持证书校验启用。
aiohttp 保持 Cookie 的 Secure 和 host-only 边界，并在处理超大过期时间时
保持客户端连接可复用；multidict 在 items-view 集合运算后释放临时引用。

## 破坏性操作

PAS 执行能力校验和确认要求，但 MySQL 账号仍是最终数据库权限边界。确认标志
不会授予缺失权限。即使允许其他可确认语句，`DROP DATABASE` 仍可能保持阻止。

## 保留与导出

根据组织策略和可用存储配置审计与应用日志保留时间，在到期前通过批准的渠道
导出。即使已经移除密钥，元数据仍可能具有运维敏感性，因此应限制日志、备份
和支持包访问。
