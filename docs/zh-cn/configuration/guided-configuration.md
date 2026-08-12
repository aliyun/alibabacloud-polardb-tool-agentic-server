# 引导式模块化配置

[English](../../en/configuration/guided-configuration.md) | **简体中文**

本文介绍服务启动后的可选模块和安全配置变更。

## 开始之前

请先完成[初始化设置](../setup/initial-setup.md)。该指南定义
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY` 启动契约、数据库迁移、首个
管理员、Docker 与 Kubernetes token 交付，以及恢复流程。

本文假定 setup UI 或 `pas config init` 已经完成接管。管理员可以在控制台打开
**服务配置**（`/settings/configuration`）查看或调整模块。完成接管后访问
`/setup` 会跳转到
这个需要管理员认证的页面。也可以使用下面的交互式和声明式 CLI 命令。

## 模块与依赖

`core_admin` 和 `token_security` 分别建立管理员与共享 JWT 密钥环。
`agent_token_auth`、`runtime_policy`、`sql_security`、`observability`
使用已物化的安全默认值并默认激活。

其他能力均为模块化选项：

- `agent_token_auth` 是必需的内置能力，为 MCP 和 Agent REST API 签发并认证
  由管理员管理的 Agent Bearer Token。它与人类管理员登录无关，不能跳过或停用。
- `user_sso` 启用人类用户 OIDC 登录，也可保持 `SKIPPED`。
- `aliyun_access` 保存加密的阿里云访问凭证、地域，以及
  `openapi_network`（`public` 或 `vpc`），用于选择经过审核的 PolarDB 与
  STS OpenAPI 公网或 VPC 端点族。它支持 `direct_ak`、`assume_role` 和
  `ecs_ram_role` 三种凭证模式。
自动供给池的网络位置、容量、权限与 Agent 路由不是配置模块。服务端内置的
`agentic-dedicated-mysql` profile 是 AgenticDB Dedicated 固定
`CreateDBCluster` 参数的唯一来源；每个资源池只保存地域、可用区、VPC、VSwitch
和允许选择的存储类型。激活
`aliyun_access` 后在 **Pool** 页面管理资源池。网络标识需要手工填写，因为 PAS
不会为了枚举网络资源而额外申请 RAM 权限。

**服务运行策略**（`runtime_policy`）保存 PAS 全局运行设置，其中包括
`dedicated_pool_enabled`。自动供给 Worker 未启用或没有新鲜心跳时，资源池向导会
直接链接到 `/settings/configuration?module=runtime_policy`。

可选模块可保持 `SKIPPED`，以后再配置。例如，可以跳过 `user_sso`，完全采用
内置 Agent Bearer Token 能力。停用已激活的可选模块时会执行依赖感知的安全
停用规则，不等同于删除其已存配置。

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
    credential_mode: assume_role
    region_id: cn-hangzhou
    openapi_network: public
    assume_role:
      source_access_key_id_from_env: PAS_STS_SOURCE_ACCESS_KEY_ID
      source_access_key_secret_from_env: PAS_STS_SOURCE_ACCESS_KEY_SECRET
      role_arn: acs:ram::<account-id>:role/pas-runtime
      role_session_name: polardb-agentic-a1b2c3d4
      external_id_from_env: PAS_STS_EXTERNAL_ID
```

使用 `direct_ak` 时，在 `direct_ak` 块中填写 `access_key_id_from_env` 和
`access_key_secret_from_env`。使用 `ecs_ram_role` 时，填写 `ecs_ram_role`
块并可选填写 `role_name`；该模式不接受 AccessKey。声明中的所有密钥值都必须
使用 `_from_env`（或受支持的 `_from_file`、`_from_stdin`），不能写入 YAML
明文。PAS 会加密保存固定凭证，`temporary credentials`（临时凭证）只存在于
进程内存，绝不会导出到 Agent、sandbox、浏览器或元数据库。

当服务 Pod 具备阿里云 VPC 连通性但没有公网出口时，将
`openapi_network` 设置为 `vpc`。PolarDB 与 STS 都会使用对应地域的 VPC
端点。默认值为 `public`；系统会有意拒绝自定义端点域名。

dry run 与验证请求均由 PAS 后端 Pod 发起，因此该 Pod 必须能够解析并路由到
所选端点。后端会发送只读 PolarDB 元数据请求；AssumeRole 模式会先获取 STS
凭证。UI 会显示实际解析出的端点，并且只持久展示以下稳定、脱敏的错误码：

- `OPENAPI_DNS_FAILURE`
- `OPENAPI_CONNECT_FAILURE`
- `OPENAPI_TLS_FAILURE`
- `OPENAPI_ENDPOINT_UNSUPPORTED`
- `OPENAPI_CREDENTIAL_INVALID`
- `OPENAPI_PERMISSION_DENIED`
- `OPENAPI_STS_SOURCE_CREDENTIAL_INVALID`
- `OPENAPI_STS_ASSUME_ROLE_DENIED`
- `OPENAPI_STS_ROLE_TRUST_REJECTED`
- `OPENAPI_STS_EXTERNAL_ID_MISMATCH`
- `OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED`
- `OPENAPI_ECS_METADATA_DISABLED`
- `OPENAPI_ECS_IMDSV2_UNAVAILABLE`
- `OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED`

原始 SDK 异常和已配置凭证不会返回浏览器。每个稳定码对应的模式专属处理措施，请参阅
[故障排查](../operations/troubleshooting.md)。

应用前必须先检查计划：

```bash
pas config apply --file onboarding.yaml --dry-run
pas config apply --file onboarding.yaml
```

dry run 会完成解析、规范化、Schema 检查、依赖规划和非变更验证；不会保存
草稿、激活模块、消费 bootstrap token 或创建云资源。

## 阿里云凭证模式

为当前有效的 `aliyun_access` 配置选择一种凭证模式：

- `direct_ak` 使用长期 AccessKey 对，主要适用于受控的开发、迁移或恢复场景。
- `assume_role` 只在 PAS 保存专用、低权限的源 AccessKey，再扮演 RAM 角色，
  获取可自动续期的临时身份凭证（STS Token）。可选的 External ID 是 Secret
  输入，不是展示字段。
- `ecs_ram_role` 不保存 AccessKey。PAS 必须部署在已授予该 RAM 角色的 ECS
  实例上。运行时仅使用 IMDSv2；不会回退到 IMDSv1，也不允许用户配置元数据 URL。

网络可用时，应在 **Save and enable** 前运行 **Test connection**。成功的 dry
run 是推荐项而非必需项；验证失败或不可用后保存时，必须进行显式 confirmation。
结果只显示所选模式、端点决策、脱敏身份或角色、适用时的临时到期时间、稳定错误
码和阿里云 Request ID。

## 导出、备份与恢复

导出只返回有效配置，密钥字段仅显示已配置或脱敏标记：

```bash
pas config export --file effective.yaml
pas config export --module runtime_policy --file runtime-policy.yaml
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
