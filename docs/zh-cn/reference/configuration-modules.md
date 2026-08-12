# 配置模块参考

[English](../../en/reference/configuration-modules.md)

运行时配置以加密、带 revision 的模块文档存储在元数据库中。只有
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY` 保留为进程启动配置。

## 模块目录

- `token_security`：共享 JWT 密钥环和 Token 生命周期。
- `core_admin`：首个内置管理员，依赖 `token_security`。
- `agent_token_auth`：为 MCP 和 Agent REST API 签发并认证 Agent Bearer Token
  的必需内置能力，默认激活，与人类管理员认证无关，不能跳过或停用。
- `user_sso`：可选 OIDC 人类登录，依赖 `token_security`。
- `aliyun_access`：按模式划分的阿里云凭证、地域和 `openapi_network`。
- `runtime_policy`：外部 URL、CORS、连接池、Worker 策略、全局
  `delete_cooldown_duration_hours` 和 `dedicated_pool_enabled` 激活 Flag。
- `sql_security`：限制、阻止操作、确认、限流和审计。
- `observability`：日志和审计保留行为。

可选模块可以保持 `SKIPPED`。停用依赖项前先停用所有有效下游模块。

自动供给池的网络位置、容量、权限和路由是在 **Pool** 与 **Agent** 页面管理的
资源，不是配置模块。旧 `resource_pool` 模块已经退役，配置命令会拒绝该模块名。

服务端内置的 `agentic-dedicated-mysql` profile 是 AgenticDB Dedicated 集群固定
`CreateDBCluster` 参数的唯一来源。资源池选择网络位置及 profile 允许的存储类型；
管理员不再维护第二套购买参数模块。

## 自动供给 Worker 运行安全

**服务运行策略**（`runtime_policy`）模块提供以下自动供给 Worker 控制项：

- `dedicated_pool_enabled` 启动或停止真实 Dedicated 生命周期任务。每个副本的
  进程内监督任务会在线应用有效 Revision，修改后无需重启或滚动 PAS。
- `dedicated_worker_heartbeat_interval_seconds` 默认为 10 秒。
- `dedicated_worker_heartbeat_stale_after_seconds` 默认为 30 秒，并且必须同时不少于
  三个心跳间隔和 30 秒。心跳写入和新鲜度比较使用元数据库时钟。
- `dedicated_pool_simulation_enabled` 默认为 false。它是显式的开发/测试回退开关，
  仅在没有有效阿里云凭证时创建带标记的模拟成员；有效凭证始终优先。该值为 false
  时，缺少阿里云访问配置会 fail closed；生产环境不得保留该开关。
- `dedicated_pool_preparation_mode` 默认为 `full`。`openapi_only` 会创建真实且
  计费的云资源，但在私网 MySQL 授权与验证前停在持久化 `OPENAPI_READY`。切回
  `full` 后，无需重启 PAS 或重复购买，Worker 会在线继续这些成员。

在这个暂停边界，资源池成员保持 `REPLENISHING`。如果该成员来自 Agent 冷创建
请求，Agent 侧数据库资源也保持 `CREATING`，不得返回凭证或参与分配；底层
PolarDB 集群仍然已经运行并产生费用。

控制台就绪接口会报告持久化的活跃 Worker 证据、模拟模式和准备模式。新启用的 Worker 可能需要
一个心跳间隔才会显示为活跃。启用模块并不能证明其阿里云身份具有
`CreateDBCluster` 权限。

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

## 阿里云凭证模式

`aliyun_access` 只有以下三种模式：

- `direct_ak` 直接使用已加密保存的长期 AccessKey ID 和 Secret。
- `assume_role` 在 PAS 加密保存低权限源 AccessKey ID 和 Secret，再扮演
  RAM 角色，使用临时身份凭据（STS Token；SDK 文档中也称
  `temporary credentials`）发起 OpenAPI 调用。
- `ecs_ram_role` 不保存 AccessKey。PAS 必须部署在已授予该 RAM 角色的
  ECS 实例上。运行时仅使用 IMDSv2；不支持 IMDSv1 回退、任意元数据 URL
  或第二段角色扮演链。

运行时只解密选中的块。临时凭证只留在进程内存，绝不会持久化、出现在 API
响应、导出或日志中，也不会发送给 Agent 或 sandbox。AccessKey ID 在静态保存时
同样会加密，并使用服务端生成的短 `display mask`；AccessKey Secret 和 External
ID 只显示“已配置”状态。dry run 结果可以包含脱敏身份或角色、临时到期时间和安全
的阿里云 Request ID。

当当前模式存在已保存凭据时，切换模式需要 confirmation。默认选择是
**Clear the previous credential**。
**Retain it, but keep it disabled** 会加密保留旧块但使其失效；该块不会被解密或
自动复用。要重新激活时，选择 **Use retained credential** 或输入替换值，然后
验证并显式启用。**Delete retained credential** 会永久删除该非活动块。将保留的
direct AccessKey 复用为 AssumeRole 的源凭证同样是独立、需要确认的操作。

`direct_ak` 的 dry run 只读取 PolarDB 元数据。`assume_role` 会先执行
AssumeRole，再使用临时凭证读取 PolarDB。`ecs_ram_role` 会先经由 IMDSv2 获取
ECS 角色，再进行同样的只读检查。PolarDB 预检调用 `DescribeDBClusters`，只能
证明凭据具备集群查询权限；它不会验证自动供给池购买所需的
`CreateDBCluster` 等权限。启用自动供给前请授予这些权限，否则资源池可能
无法自动创建实例。
`OPENAPI_PERMISSION_DENIED` 表示活动凭证已连接到 PolarDB 但缺少所需授权；
请按故障排查指南操作，不要盲目扩大权限。

## 密钥与导出

Secret 字段在根密钥下独立加密。省略已有 Secret 会保留原值，显式支持的清除
动作才会删除。Describe 和导出响应只包含已配置/脱敏标记。
