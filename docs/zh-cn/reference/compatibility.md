# 兼容性与版本策略

[English](../../en/reference/compatibility.md)

`0.0.1` 是首个公开试用版本，以 Pre-release 发布。用户评估期间，`0.0.x`
版本线可能修正契约。`0.1.0` 保留给试用反馈和关键缺陷解决后的首个稳定功能
版本线。

`0.0.2` 是补丁版本：当资源池目标已经满足时，会持久化陈旧占位记录清理；
当 MySQL 报告未选择数据库时，会返回 `DATABASE_REQUIRED`；仅使用 Agent
Token 的部署也可以在配置 HTTP VPC `external_base_url` 后安全重启。从
`0.0.1` 升级不需要执行新的元数据 Schema 迁移。

`0.0.3` 要求资源池创建显式配置 VPC 和 vSwitch，强化 SQL 与凭证敏感路径，
更新依赖安全基线，并增加带签名的多架构发布及恢复制品。其容器不再需要
v0.0.1 的 `PYTHONPATH: /app` workaround。从 `0.0.2` 升级时元数据库
Schema head 不变，但替换 server 容器前仍必须执行发布版本的迁移命令。

## 运行时兼容性

- 源码安装的 Python 运行时：3.11 或更高版本。
- 容器平台：Linux `amd64` 和 `arm64`。
- 元数据库：MySQL 8.0 和 PostgreSQL，使用项目内置异步驱动。
- ACK 推荐元数据库服务：PolarDB MySQL 8.0。
- Kubernetes：1.27 或更高版本；Helm：3.12 或更高版本。

SQLite 只支持本地开发和测试，不支持生产多副本部署。

## 升级兼容性

数据库兼容性由 Alembic revision 决定，而不是应用版本字符串。应用 Pod 滚动
发布前，只执行一次发布版本的迁移 Job 或 `pas database migrate`。Schema
为空、落后、超前、不可用或存在歧义时，启动会 fail closed。

正向迁移后通常不能安全降级应用代码。应恢复兼容备份，而不是尝试自动降级。

## API 与配置

引导式配置 API 当前使用 `protocol_version: 1`；已存模块文档拥有独立 Schema
版本。客户端不能假定未来协议可读取未知模块 Schema。MCP 工具可见性取决于
授权，并可能在重新连接后变化。

## 发布制品

使用不可变 Git Tag、镜像 digest、校验和与 Attestation。已发布 Tag 或制品
绝不替换；修复使用新的 patch 版本。已部署版本的文档链接应指向对应发布 Tag。
