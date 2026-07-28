# 功能使用①：引导式配置

[English](../../en/getting-started/configure.md) | **简体中文**

完成 Owner 认领后，进入控制台配置阿里云凭证与购买规格。本页覆盖运行资源池
所需的最小配置。

## 打开配置页

以管理员登录后，打开 `/settings/configuration`。可选模块可分步配置，每次
修改先生成草稿，验证通过后再激活。

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

## 配置 agentic_db_purchase

设置创建集群时使用的购买规格（引擎版本、节点规格、代理、Serverless 弹性与
存储等）。试用可先采用默认值，后续按需调整。

## 配置 resource_pool

设置网络位置与资源池参数：

- `region_id` 与 `zone_id` 为必填项。
- `vpc_id` 与 `vswitch_id` 可选；留空时阿里云会在账号默认 VPC 中创建集群。

<p align="center">
  <img src="images/configure-resource-pool.png" alt="resource_pool 配置表单" width="820">
</p>

## 深入阅读

模块依赖、声明式 apply、导出与热加载等细节，请参阅
[引导式模块化配置](../configuration/guided-configuration.md)。

下一步：[功能使用②：注册数据库实例](./register-instance.md)。
