# 生产网络

[English](../../en/deployment/networking.md)

所有连通性测试和 SQL 转发都由 PAS 后端 Pod 发起，而不是管理员浏览器。每个
副本都必须具备一致的 DNS、路由、安全组和数据库白名单访问能力。

## 阿里云 OpenAPI

根据 Pod 网络配置 `aliyun_access.openapi_network`：

- `public`：`polardb.<region>.aliyuncs.com` 和
  `sts.<region>.aliyuncs.com`。
- `vpc`：`polardb-vpc.<region>.aliyuncs.com` 和
  `sts-vpc.<region>.aliyuncs.com`。

AssumeRole 同时依赖 STS 和 PolarDB。纯 VPC 环境需要确认 CoreDNS 能通过
阿里云 DNS 或 PrivateZone 解析 VPC 端点，并确认路由和安全策略允许 HTTPS
`443` 端口。系统不接受自定义端点域名。

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
