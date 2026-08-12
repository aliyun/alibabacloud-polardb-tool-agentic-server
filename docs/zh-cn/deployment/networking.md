# 生产网络

[English](../../en/deployment/networking.md)

所有连通性测试和 SQL 转发都由 PAS 后端 Pod 发起，而不是管理员浏览器。每个
副本都必须具备一致的 DNS、路由、安全组和数据库白名单访问能力。

## 阿里云 OpenAPI

根据 Pod 网络配置 `aliyun_access.openapi_network`：

- `public`：resolver 对中央地域（`cn-hangzhou`、`cn-shanghai`、
  `cn-beijing`、`cn-wulanchabu`、`cn-heyuan`、`cn-hangzhou-finance` 和
  `cn-beijing-finance-1`）使用全局主机名 `polardb.aliyuncs.com`；其他地域
  使用 `polardb.<region>.aliyuncs.com`。STS 仍使用地域端点
  `sts.<region>.aliyuncs.com`。
- `vpc`：`polardb-vpc.<region>.aliyuncs.com` 和
  `sts-vpc.<region>.aliyuncs.com`。

AssumeRole 同时依赖 STS 和 PolarDB。纯 VPC 环境需要确认 CoreDNS 能通过
阿里云 DNS 或 PrivateZone 解析 VPC 端点，并确认路由和安全策略允许 HTTPS
`443` 端口。系统不接受自定义端点域名。

## AssumeRole 与 ECS 要求

对于 `assume_role`，请为 PAS 创建专用源身份。其 source policy 只应在 PAS
使用的一个目标角色上授予 `sts:AssumeRole`，不要授予通配 Resource，也不要向该
源身份授予 PolarDB 权限：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "acs:ram::<account-id>:role/pas-runtime"
    }
  ]
}
```

在目标角色上配置 trust policy，只允许预期的源 principal AssumeRole。如果 PAS
提供 External ID，应在目标 trust policy 的 condition 中要求相同值。将所需的
PolarDB OpenAPI 权限附加到目标角色，而不是源身份。源 AccessKey 只保留在 PAS；
STS temporary credentials 不会持久化。

对于 `ecs_ram_role`，将目标 RAM 角色绑定到运行 PAS 后端 Pod 的每台 ECS 实例。
PAS 通过官方 SDK 调用固定的 ECS metadata service，并且仅使用 IMDSv2。允许实例
需要的 Pod 到元数据路径，但必须保持 no HTTP proxy，不要暴露 metadata URL
配置，或期待 IMDSv1 fallback。启用该模式前，应在每个副本验证角色绑定和 IMDSv2
访问。

## 数据库端点

注册实例的 **Test Connection**、凭证测试、供应 DDL 和 Agent SQL-over-HTTP
请求都使用处理请求的 PAS Pod 网络路径。应允许所有可能的 Pod 源地址访问
MySQL `3306` 端口，并在节点池或 VPC 变更时同步白名单。跨 VPC 实例应使用
CEN 或经过批准的私网连接。

## Ingress 与 TLS

只有显式启用时 Chart 才会创建 Ingress。Ingress Controller、公网或私网负载
均衡、DNS 记录、TLS 证书、请求大小/超时策略和来源限制均由运维方负责。只在
经过批准的边界终止 TLS；启用 SSO 前，应把外部基础 URL 配置为实际 HTTPS
地址。
