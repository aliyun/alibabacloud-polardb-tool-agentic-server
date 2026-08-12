# 健康与就绪

[English](../../en/operations/health-and-readiness.md)

Kubernetes 和外部监控应区分进程存活与流量就绪。

## 端点

`GET /livez` 只确认进程事件循环存活，不证明数据库、配置、OpenAPI 或已注册
实例的连通性。

`GET /readyz` 比较 Pod 已加载配置版本与共享元数据库版本。数据库版本无法
读取、Pod 配置落后或必需重载失败时返回 `503`。setup 模式下成功就绪仍返回
`200`，响应中的 `mode` 区分 `SETUP` 和 `READY`。

`GET /healthz/dependencies` 是有限的依赖摘要。外部服务应使用显式连接和
OpenAPI 验证流程。

## 多副本行为

配置写入会递增共享版本。每个 Pod 轮询并原子替换运行时快照，只有已加载版本
达到当前值时才就绪。默认间隔为五秒。可选模块重载失败与必需失败会分开报告。

## Dedicated 成员就绪

应用就绪端点不表示每个自动供给池成员都可分配。Dedicated 成员具有独立的 `FRESH`、
`STALE` 或 `CHECKING` 证据。PAS 只分配证据年龄在自动供给池上限内、同时处于
`AVAILABLE` + `FRESH` 的成员。

证据过期会排除成员并安排复检，但不会仅凭过期就隔离成员或触发替代购买。只有
结论明确的复检失败才转为 `QUARANTINED`；不确定的 Worker 或网络故障保持 stale
并重试。最大证据年龄必须至少为检查间隔的两倍。

## 告警

应对持续就绪失败、重启循环、迁移 Job 失败、数据库连接耗尽、供应失败和连续
认证拒绝告警。自动供给池还应针对持续 planning 缺口、stale/checking 或
quarantined 增长、验证失败、成员硬上限耗尽和补购速度告警。只采集响应代码和
脱敏类别，不采集密钥或完整连接字符串。
