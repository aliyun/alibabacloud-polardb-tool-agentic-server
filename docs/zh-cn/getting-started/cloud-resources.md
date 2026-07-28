# 资源要求

[English](../../en/getting-started/cloud-resources.md) | **简体中文**

本页指导你准备两项云资源：运行 PAS 的 ECS，以及作为 PAS 元数据库的
PolarDB MySQL 集群。完成后你将得到部署阶段所需的连接信息。

## 你将获得什么

- 一台可公网访问的 ECS，用于运行 Docker Compose 部署的 PAS。
- 一套 PolarDB MySQL 8.0 集群，作为 PAS 的元数据库（存储配置、凭证与密钥
  环等元数据，不是被 Agent 使用的目标业务库）。
- 一条可用的 `PAS_DATABASE_URL` 连接串。

## 第一步：购买 ECS 实例

在[阿里云 ECS 控制台](https://ecs.console.aliyun.com/home)创建实例，建议：

- 地域：选择与 PolarDB 元数据库相同的地域与可用区，便于内网互通。
- 规格：试用场景 2 vCPU / 4 GiB 起步即可。
- 系统盘：40 GiB 及以上的云盘。
- 镜像：主流 Linux 发行版（如 Alibaba Cloud Linux / Ubuntu）。
- 公网：分配公网 IP 或绑定 EIP，便于试用期访问控制台。
- 安全组：放通 SSH（TCP 22）与 PAS 端口（TCP 18760）的入方向来源，建议
  仅对你的办公网段开放。

## 第二步：购买 PolarDB MySQL 作为元数据库

在 [PolarDB 控制台](https://polardb.console.aliyun.com/overview)创建
PolarDB MySQL 8.0 集群，并完成：

- 地域与 VPC 同第一步购买的 ECS 保持一致。
- 实例规格推荐企业版 2C8G（`polar.mysql.x4.medium`），后续有容量需求可以
  对实例升配。

<p align="center">
  <img src="images/polardb-purchase.png" alt="PolarDB 购买页配置" width="820">
</p>

- 创建一个数据库账号（用户名 + 密码），例如账号名 `pas`。
- 创建一个空数据库（例如 `pas_meta`）用于存放 PAS 元数据，并授权该账号
  访问该数据库。

<p align="center">
  <img src="images/polardb-create-database.png" alt="创建数据库并授权账号" width="820">
</p>

- 记录集群的连接地址（Endpoint）与端口（默认 3306）。

## 第三步：打通网络与账号

- 若 ECS 与 PolarDB 在同一 VPC，优先使用内网 Endpoint。
- 将 ECS 的内网/公网地址加入 PolarDB 的访问白名单。
- 在 ECS 上用 MySQL 客户端验证连通性，确认账号可登录目标数据库。

<p align="center">
  <img src="images/ecs-mysql-connect.png" alt="在 ECS 上验证连通性" width="820">
</p>

## 产出清单

进入部署阶段前，请确认已获得：

- ECS 公网地址与 SSH 登录方式。
- PolarDB 的 Endpoint、端口、账号、密码、数据库名。
- 据此拼出的连接串（注意：密码中的特殊字符需要做 URL 编码转义，例如
  `@` 写作 `%40`）：

```text
mysql+asyncmy://USER:PASSWORD@ENDPOINT:3306/DATABASE
```

下一步：[部署（单台 ECS + Docker Compose）](./deploy-compose.md)。
