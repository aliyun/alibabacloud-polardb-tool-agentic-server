# 日志与可观测性

[English](../../en/operations/logs-and-observability.md)

PAS 将结构化进程日志写入 stdout，也可以在 `/app/log` 下维护轮转持久日志。
容器平台应连同 Pod/容器身份采集 stdout。

## 启动与迁移

迁移执行器日志应与应用日志分开保留，无论执行器是 Compose 容器、Helm Job
还是托管 one-shot Pod。启动日志记录 Schema 门禁、引导初始化就绪和脱敏配置
重载结果。首次 bootstrap token 可能只在 stdout 出现一次，因此应限制初始
日志访问和保留。

## 运行时信号

监控 HTTP 状态和延迟、MCP 工具结果、SQL 策略阻止、认证失败、配置版本延迟、
数据库连接池压力、供应队列时长、生命周期失败和审计保留任务。指标标签不能
包含高基数 SQL、Token、账号或凭证。

Dedicated 信号包括分配热命中/冷缺失和时长；allocatable、planning、billable、
surplus、stale、checking、quarantined 容量；购买预留与补购速度；限速/容量 Guard
拒绝；就绪结果；删除/断连/冷却/恢复/清理结果；权限同步；以及省略 MCP 供应模式。
标签只使用有界 signal/outcome/kind 值。自动供给池名称、成员 ID、Agent 名称、数据库名、
用户名、Endpoint、Token 和原始错误都不能作为指标标签。
PolarRAG Tool 治理会发出 `polarrag_tool_rejections_total`，标签为 `tool` 和
脱敏的内部 `reason`；还会发出 `polarrag_tool_instance_inflight`，标签为
12 字符哈希 `instance_scope`。应对持续增长的拒绝数和接近配置上限的在途值
告警。不要把 `instance_scope` 当作可逆的实例标识。

已恢复满容量的空闲用户和 Agent 速率桶会被周期清理，进程本地桶表也有硬容量限制。
这会在身份频繁变化时约束内存，同时保持文档所述的单副本近似限流模型。
治理器只移除已经恢复满容量的桶；当有效桶占满容量时，新身份按常规限流响应
fail-closed，不会通过重置已有身份预算来腾出空间。

每个到达治理环节的已认证 PolarRAG Tool 执行都使用现有 `polarrag.<tool>` 审计
动作。治理拒绝记录只包含稳定错误、公开原因、重试间隔，以及适用时的请求扇出数和
当前在途数；拒绝审计载荷不会加入调用方资源 ID、用户 ID、Agent ID、原始实例 ID、
ACL 上下文或上游细节。一次 `429` 必须对应该被拒请求新增上游调用数为零。

## 脱敏

绝不能记录 AccessKey、密码、Agent Token、bootstrap token、Cookie、密文、
SQL 参数值或包含密钥的完整异常。使用稳定错误码和请求标识关联事件。

## 保留

通过有效的可观测性/安全配置设置日志目录、轮转大小、备份数量、时区和审计
保留时间。临时卷应满足所选策略容量，或在 Pod 替换前把日志发送到外部。

## Dedicated 告警解释

`stale_excluded` 就绪信号不是购买或成员故障。应对其持续增长和 Worker Lag 告警，
但评估购买速度时只统计 `replenishment/purchase_reserved`。比较 `billable_total`
与 `max_total_members`；冷却和删除中的成员仍计费，即使它们不满足 planning 容量。
