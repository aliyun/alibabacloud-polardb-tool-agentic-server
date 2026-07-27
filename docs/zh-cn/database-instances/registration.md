# 注册数据库实例

[English](../../en/database-instances/registration.md)

物理数据库注册统一收敛到 **Instances**。部门、用户和 Agent 选择已有注册，
不再重复填写连接四元组。

## 必填字段

注册时填写集群 ID、显示名称、引擎、拓扑、可选地域和用途，以及 host、port、
用户名和密码。PolarDB MySQL 默认端口为 `3306`。分配模式固定为
`registered`，不作为选项展示。

用途文本会由 `list_db_instances` 和 `describe_db_instance` 返回，帮助 Agent
选择正确工作负载，同时不改变稳定的实例 UUID。

## 保存前测试

**Test Connection** 从 PAS 后端 Pod 发起，使用完整 host、port、用户名和密码
执行 `SELECT 1`。结果会持续显示在按钮下方。任何连接字段变化都会使旧结果
失效，必须重新测试。

`multitenant` 注册还会检查 `enable_multi_tenant=ON`，并验证已配置用户名存在
于 `rds_kill_user_list`。连接成功并不代表这些供应前提一定满足。

## 编辑与轮换

详情页可以修改显示名称、用途、地域、host 和 port。host 或 port 改变后必须
重新执行连接测试。集群 ID、引擎、拓扑和分配模式是不可变身份字段。

凭证单独管理。可以添加、测试、编辑或吊销凭证，在不替换实例注册的情况下
轮换后端密码；已有绑定继续引用该凭证记录。

## 权限边界

MCP 只能访问所选 MySQL 账号允许的数据库。PAS 不会绕过或提升 MySQL 授权。
直连访问和供应管理应使用相互独立的最小权限凭证。
