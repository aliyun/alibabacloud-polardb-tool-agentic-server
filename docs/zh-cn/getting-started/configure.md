# 功能使用①：引导式配置

[English](../../en/getting-started/configure.md) | **简体中文**

完成 Owner 认领后，进入控制台配置阿里云凭证与服务运行策略。本页覆盖自动供给池
能够购买集群前所需的服务级配置。

## 打开配置页

以管理员登录后，打开**服务配置**（`/settings/configuration`）。可选模块可分步
配置，每次修改先生成草稿，验证通过后再激活。

<p align="center">
  <img src="images/configuration-modules.png" alt="配置模块列表" width="820">
</p>

## 配置 aliyun_access

填写阿里云访问凭证与地域：

- `access_key_id` / `access_key_secret`：具备 PolarDB 集群管理权限的 RAM 凭证。
- `region_id`：目标地域。
- `endpoint_network`：选择 `public`（公网）或 `vpc`（内网）以决定调用
  PolarDB OpenAPI 的接入网络。若 PAS 运行在与 PolarDB 同一 VPC，建议选择
  `vpc`。

<p align="center">
  <img src="images/configure-aliyun-access.png" alt="aliyun_access 配置表单" width="820">
</p>

## 检查服务运行策略

打开**服务运行策略**并启用自动供给 Worker。Worker 未运行时，资源池向导会链接到
`/settings/configuration?module=runtime_policy`。固定 `CreateDBCluster` 参数不在
这里配置；服务端内置的 `agentic-dedicated-mysql` profile 是唯一来源。

## 创建自动供给池

资源池配置不是服务配置模块。打开 **Pool**，创建**自动供给池（AgenticDB
Dedicated）**并填写网络位置和容量：

- `region_id` 与 `zone_id` 为必填项。
- `vpc_id` 与 `vswitch_id` 均为必填项。PAS 无法自动识别承载自身的 ECS、
  容器或 Kubernetes 环境所在的 VPC，因此不会使用阿里云账号的默认 VPC。
- 请填写 PAS 可达的 VPC，并选择该 VPC、目标可用区中的 VSwitch。通常应与
  PAS 部署在同一 VPC；如果使用不同 VPC，必须先通过云企业网、VPC 对等连接
  等方式打通网络。
- 使用表单根据地域生成的 VPC 控制台链接，手工复制 VPC 和 VSwitch 标识。该设计
  不要求额外的 RAM 网络资源枚举权限。
- 配置目标容量、成员硬上限、购买预算、权限 Revision、回收策略和冷却时间。
  只有自动供给 Worker、阿里云身份、内部购买 profile 和默认权限版本都就绪后，
  PAS 才会向目标容量准备实例。

控制台可以列出多个自动供给池。在 Agent 详情页绑定一个
主池和可选的有序回退池。

## 深入阅读

模块依赖、声明式 apply、导出与热加载等细节，请参阅
[引导式模块化配置](../configuration/guided-configuration.md)。
资源池字段、就绪阻塞原因、路由和冷创建详见
[自动供给池](../database-instances/dedicated-hot-pools.md)。

下一步：[功能使用②：注册数据库实例](./register-instance.md)。
