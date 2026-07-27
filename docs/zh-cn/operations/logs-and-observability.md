# 日志与可观测性

[English](../../en/operations/logs-and-observability.md)

PAS 将结构化进程日志写入 stdout，也可以在 `/app/log` 下维护轮转持久日志。
容器平台应连同 Pod/容器身份采集 stdout。

## 启动与迁移

迁移 Job 日志应与应用日志分开保留。启动日志记录 Schema 门禁、引导初始化
就绪和脱敏配置重载结果。首次 bootstrap token 可能只在 stdout 出现一次，
因此应限制初始日志访问和保留。

## 运行时信号

监控 HTTP 状态和延迟、MCP 工具结果、SQL 策略阻止、认证失败、配置版本延迟、
数据库连接池压力、供应队列时长、生命周期失败和审计保留任务。指标标签不能
包含高基数 SQL、Token、账号或凭证。

## 脱敏

绝不能记录 AccessKey、密码、Agent Token、bootstrap token、Cookie、密文、
SQL 参数值或包含密钥的完整异常。使用稳定错误码和请求标识关联事件。

## 保留

通过有效的可观测性/安全配置设置日志目录、轮转大小、备份数量、时区和审计
保留时间。临时卷应满足所选策略容量，或在 Pod 替换前把日志发送到外部。
