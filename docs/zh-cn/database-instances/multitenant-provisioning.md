# 多租户供应

[English](../../en/database-instances/multitenant-provisioning.md)

已注册的 `multitenant` PolarDB MySQL 实例可以成为部门或显式授权 Agent 的
供应后端。

## 前提

注册预检要求连接成功、`enable_multi_tenant=ON`，并且供应管理员存在于
`rds_kill_user_list`。凭证需要后端所需的 PolarDB 租户和资源控制权限。集群
启用方式以官方 PolarDB 多租户文档为准。

直连访问仍是独立用途。多租户实例可以为已有数据库配置 `direct_access`
凭证，并为生命周期 DDL 配置 `provisioning_admin` 凭证；普通 SQL 不要复用
管理员凭证。

## 后端策略

配置容量、CPU 范围、DDL 并发、优先级和生命周期状态。`active` 接受放置，
`draining` 阻止新放置但保留清理能力，`disabled` 为恢复保留后端。容量控制
放置，但绝不会隐藏 Agent 已有的自有资源。

## Agent 供应

在有效、健康的后端上授予 **Create managed databases**。Agent 随后获得
`create_db_instance`，直连 SQL 能力仍为可选。创建会自动建立数据库账号并
加密保存连接信息；只有资源达到 `READY` 后，
`describe_db_instance` 才向拥有且已授权的 Agent 返回凭证。

## 生命周期与恢复

创建和删除是异步、幂等操作。应观察 `CREATING`、`READY`、
`CREATE_FAILED`、`DELETING`、`DELETED` 和 `DELETE_FAILED`。
`client_token` 会永久关联规范化请求。诊断脱敏失败和后端状态后，通过管理员
流程重试失败清理。
