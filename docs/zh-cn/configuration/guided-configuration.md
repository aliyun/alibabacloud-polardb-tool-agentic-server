# 引导式模块化配置

[English](../../en/configuration/guided-configuration.md) | **简体中文**

本文介绍服务启动后的可选模块和安全配置变更。

## 开始之前

请先完成[初始化设置](../setup/initial-setup.md)。该指南定义
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY` 启动契约、数据库迁移、首个
管理员、Docker 与 Kubernetes token 交付，以及恢复流程。

本文假定 setup UI 或 `pas config init` 已经完成接管。管理员可以在控制台打开
`/settings/configuration` 查看或调整模块。完成接管后访问 `/setup` 会跳转到
这个需要管理员认证的页面。也可以使用下面的交互式和声明式 CLI 命令。

## 模块与依赖

`core_admin` 和 `token_security` 分别建立管理员与共享 JWT 密钥环。
`runtime_policy`、`sql_security`、`observability` 使用已物化的安全默认值。

其他能力均为模块化选项：

- `agent_token_auth` 启用由管理员签发的 Agent Token。
- `user_sso` 启用人类用户 OIDC 登录，也可保持 `SKIPPED`。
- `aliyun_access` 保存加密的阿里云访问凭证、地域，以及
  `openapi_network`（`public` 或 `vpc`），用于选择经过审核的 PolarDB 与
  STS OpenAPI 公网或 VPC 端点族。
- `agentic_db_purchase` 依赖 `aliyun_access`，持有 PolarDB
  集群购买规格（引擎版本、节点规格、代理、Serverless 弹性与存储），
  池化与专属建集群共用同一份规格。
- `resource_pool` 依赖 `agentic_db_purchase`，持有网络位置以及资源池
  容量与补充行为。`region_id` 与 `zone_id` 为必填；`vpc_id` 与
  `vswitch_id` 也必须填写，并指向 PAS 可达的 VPC 与 VSwitch。PAS
  无法识别自身部署环境所在的 VPC，因此不会使用阿里云账号的默认 VPC。

可选模块可保持 `SKIPPED`，以后再配置。例如，可以跳过 `user_sso`，完全采用
管理员颁发 Agent Token 的模式。停用已激活模块时会执行依赖感知的安全停用
规则，不等同于删除其已存配置。

## 终端交互流程

通过模块列表逐项继续配置：

```bash
pas config modules
pas config configure user_sso
pas config skip user_sso
pas config show runtime_policy
```

每次编辑都会创建草稿。验证会检查语法、依赖和外部连通性，但不改变当前有效
配置。激活必须携带新的验证凭据和预期 revision，从而避免多个管理员静默覆盖。

## 声明式流程与 dry run

密钥引用统一采用 `<field>_from_env` 约定。CLI 在本地读取对应环境变量，通过
认证连接发送密钥，YAML 中不保存明文。

```yaml
protocol_version: 1
core_admin:
  desired_state: active
  config:
    username: admin
    password_from_env: PAS_SETUP_ADMIN_PASSWORD
user_sso:
  desired_state: skipped
aliyun_access:
  desired_state: active
  config:
    credential_mode: direct_ak
    access_key_id_from_env: ALIBABA_CLOUD_ACCESS_KEY_ID
    access_key_secret_from_env: ALIBABA_CLOUD_ACCESS_KEY_SECRET
    region_id: cn-hangzhou
    openapi_network: public
```

当服务 Pod 具备阿里云 VPC 连通性但没有公网出口时，将
`openapi_network` 设置为 `vpc`。PolarDB 与 STS 都会使用对应地域的 VPC
端点。默认值为 `public`；系统会有意拒绝自定义端点域名。

dry run 与验证请求均由 PAS 后端 Pod 发起，因此该 Pod 必须能够解析并路由到
所选端点。后端会发送只读 PolarDB 元数据请求；AssumeRole 模式会先获取 STS
凭证。UI 会显示实际解析出的端点，并且只持久展示以下脱敏错误类别：

- `OPENAPI_DNS_FAILURE`
- `OPENAPI_CONNECT_FAILURE`
- `OPENAPI_TLS_FAILURE`
- `OPENAPI_ENDPOINT_UNSUPPORTED`
- `OPENAPI_CREDENTIAL_INVALID`
- `OPENAPI_PERMISSION_DENIED`

原始 SDK 异常和已配置凭证不会返回浏览器。

应用前必须先检查计划：

```bash
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
```

dry run 会完成解析、规范化、Schema 检查、依赖规划和非变更验证；不会保存
草稿、激活模块、消费 bootstrap token 或创建云资源。

## 导出、备份与恢复

导出只返回有效配置，密钥字段仅显示已配置或脱敏标记：

```bash
pas config export --file effective.yaml
pas config export --module resource_pool --file resource-pool.yaml
```

导出文件适合评审和制作环境模板，不是密钥备份。元数据库和根密钥必须分别
备份，恢复时两者缺一不可。根密钥轮换是显式、可审计的重新加密操作，不能
仅修改 Secret 值来完成。

## 外部地址与热加载

初始化 UI 默认使用同源请求。仅使用 Agent Token、且运行在受控私网中的部署
可以配置 HTTP `runtime_policy.external_base_url`；Agent MCP 端点在重启后
仍然可用，但服务不会在这个不安全的 origin 上发布交互式 MCP OAuth 元数据。
启用 OAuth 或 OIDC 前，必须配置可信、外部可访问的 HTTPS origin。服务不会
从不可信代理请求头推断该地址。

有效配置会投影为进程内不可变快照。每个副本默认每 5 秒轮询全局版本（可配置
范围 1–60 秒），按依赖顺序加载变更；必要适配器失败时继续使用最后一个已知
正常快照。

安装处于 `SETUP` 状态时，所有可安全启动的运行时服务已经启动，但运行时访问
策略会阻断业务接口。因此，激活 `core_admin` 后，每个副本都会通过同一套版本
轮询流程切换到 `READY`，不需要额外重启。

`GET /readyz` 会比较数据库中的全局版本与当前副本已经加载的版本，并返回
`desired_config_version`、`loaded_config_version`、`config_status`、
`last_reload_error` 和 `module_errors`。副本版本落后或必要重载失败时返回
HTTP 503，Kubernetes readiness probe 可据此将该 Pod 从 Service 流量中摘除，
直到完成收敛。可选适配器失败时返回 `DEGRADED`，同时继续使用该模块上一个
有效版本。

Kubernetes 应使用 `/readyz` 作为 readiness probe。默认轮询间隔下，正常传播
时间不超过约 5 秒加数据库延迟。每个 Pod 独立判断 readiness，因此不会因为
其他 Pod 已经加载新版本，就继续把流量发送给尚未收敛的副本。

## 运维检查

使用 `pas config modules` 查看模块状态和 revision。草稿只有完成验证与激活
后才会改变有效行为。响应中的密钥字段始终脱敏，导出文件不能恢复密钥明文。

配置完成后，请继续阅读
[数据库实例访问与供应指南](../database-instances/access-and-provisioning.md)。
