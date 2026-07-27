# ACK 与 PolarDB MySQL

[English](../../en/deployment/ack-polardb.md)

阿里云 ACK 生产部署推荐使用已有的 PolarDB MySQL 8.0 集群作为 PAS
元数据库。不要在 Kubernetes 中部署 Compose 示例的 MySQL 容器。

## 数据库准备

为 PAS 元数据创建独立数据库和最小权限账号。该账号必须能够创建和修改由
Alembic 管理的表、索引和迁移元数据。每次升级前备份集群和根加密密钥。

使用所有 ACK 节点/Pod 都能访问的集群端点：

```text
mysql+asyncmy://USER:PASSWORD@POLARDB-ENDPOINT:3306/DATABASE
```

用户名和密码中的保留字符需要百分号编码。完整 URL 只能存入已有 Kubernetes
Secret。

## 网络位置

ACK 节点与 PolarDB 集群应优先位于同一地域和 VPC。根据 Pod 实际源地址配置
vSwitch 路由、安全组和 PolarDB IP 白名单。跨 VPC 时，应在安装前建立 CEN
或其他经过批准的私网路由。

创建 Secret 后，按照 Kubernetes 指南安装 Helm Chart。迁移 Hook 会在应用
Pod 滚动发布前验证数据库访问。
