# 可复用的飞书 API 回归

[English](feishu-regression.md) | **简体中文**

这套开发夹具使用真实飞书目录客户端、PAS 元数据写入、用户组访问判断以及浏览器
企业验证回调。租户、用户、凭据和响应均为虚构，无需飞书账号、云凭据或生产数据库。

## 自动化用例

在仓库根目录执行：

```shell
uv sync --locked --extra dev
uv run pytest tests/test_mock_feishu_provider.py -q
```

可复用的 FastAPI Provider 为 `mock_feishu_provider.create_app()`，测试通过
`httpx.ASGITransport` 接入，未实现的接口返回错误。覆盖多页查询、租户 `user_id`
与应用 `open_id` 的区分、部门层级、用户组成员、错误凭据、缺少分页游标、HTTP 503、
权限拒绝、回调绑定、授权码重放、离职用户访问撤销，以及拉取失败时保留旧快照。

## Chrome 控制台

```shell
(cd web && npm ci && npm run build)
uv run python scripts/dev/run_feishu_mock_console.py --confirm-local-development
```

在终端提示中输入并确认临时管理员密码（至少 8 个字符）。输入会隐藏；无法隐藏输入的
终端会被拒绝。启动器不会打印密码或 Mock App secret，也不会写入明文凭据文件。
在 Chrome 打开 `http://127.0.0.1:18765`，使用 `admin` 和刚才输入的密码登录。
可用 `--port 18766` 更换端口。每次启动均新建并迁移临时 SQLite 数据库、创建管理员、
设置回环公共地址，并将数据库和检查点放入临时目录。启动日志也可能写入本地 `log/`
目录。启动器清除继承的 PAS/云配置，
Ctrl+C 停止服务并删除临时目录，不接受已有数据库。

进入 **访问管理 > 创建 / 管理账户 > 企业身份源**，创建飞书身份源：

| 字段 | 值 |
| --- | --- |
| 来源名称 | Reusable Feishu Mock |
| App ID | `cli_mock_regression` |
| App secret | `mock-feishu-app-secret` |

创建后经过本地 Mock 授权跳转与真实 PAS 回调，租户变为 `mock-feishu-tenant`。
点击 **同步用户和用户组**，随后点击 **已同步主体**，应看到 Alice Mock、Bob Mock、
Research Mock、Platform Mock 和 Reviewers Mock。异步任务完成后刷新查看。

打开 `/login`，选择 **使用飞书登录**。Mock 授权 Alice Mock 后，真实 PAS 回调应打开
她的普通成员仪表盘，隐藏管理菜单。后续同步场景需重新使用启动器管理员登录。

仅开发启动器会在 `/__mock/feishu` 挂载 Provider。运行以下命令切换场景，再到 Chrome
中重新同步：

```shell
curl --fail http://127.0.0.1:18765/__mock/feishu/__mock/scenario \
  -H 'Content-Type: application/json' -d '{"scenario":"removed_user"}'
```

| 场景 | 预期行为 |
| --- | --- |
| `normal` | 同步两个用户、部门层级和用户组。 |
| `removed_user` | 上游移除 Bob，其目录拥有的 PAS 账户和继承访问权限失效。 |
| `second_page_failure` | 用户第二页返回 HTTP 503，不发布完成的部分目录快照。 |
| `missing_page_token` | 拒绝 `has_more=true` 却缺少游标的响应。 |
| `permission_denied` | 拒绝飞书错误码 `99991672`。 |

切回 `normal` 并同步可验证恢复。`/__mock/feishu/__mock/status` 提供脱敏后的目录
请求路径和查询字段，不记录 Token、密钥、OAuth code 或 state。服务访问日志可能
包含虚构的回调 code，请将启动器输出作为本地测试输出处理。

## 契约来源与边界

参考日期：2026-10-16。飞书官方入口：

- [租户访问凭证](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [部门直属用户](https://open.feishu.cn/document/server-docs/contact-v3/user/find_by_department)
- [子部门列表](https://open.feishu.cn/document/server-docs/contact-v3/department/children)
- [部门标识](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/contact-v3/department)
- [通用错误码](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)

夹具实现 PAS 使用的接口子集，包括用户/用户组列表和详情；每页强制只返回一条，
以覆盖分页。响应是手写契约样例，不是录制的真实租户流量或完整飞书模拟器。
部分官方文档正文依赖客户端加载，实际可执行契约在源代码和测试中明确列出。

OAuth 授权自动同意，code 在 60 秒后过期；回调仅限启动器精确的回环 PAS 地址。
启动器关闭 WebSocket 订阅，UI 中的订阅健康状态不作为验收信号，并禁止非回环出站
网络。真实租户授权、应用可见范围、服务端限流、WebSocket 事件和网络表现仍需真实
租户集成环境验证。

知识范围快照来自独立的 ETL 权限数据库，不来自这些飞书通讯录接口。本夹具不模拟
该数据库、PolarRAG 搜索/文档 ACL、钉钉或 SharePoint。已有知识权限刷新测试分别
覆盖虚构快照投影；本 Chrome 操作说明只验证飞书 HTTP 目录与浏览器登录边界。
