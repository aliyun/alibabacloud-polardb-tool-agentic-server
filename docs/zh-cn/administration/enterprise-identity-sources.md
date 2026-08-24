# 企业身份源

[English](../../en/administration/enterprise-identity-sources.md)

企业身份源会导入飞书或 SharePoint 租户的目录事实，并使其可用于已启用的 PolarRAG Space。
PAS 使用 `PAS_ENCRYPTION_KEY` 加密全部密钥，每五分钟刷新一次来源，并在服务端构造
`acl_context`。PolarRAG 仍是文档访问的最终裁决方。

控制台的**接入指南**入口先提供**飞书接入**和**SharePoint 接入**两个链接；选择后仅展示
对应来源的配置步骤，并可使用页面顶部的 `A−`、`A`、`A+` 调整阅读字号。

## 开始前

需要准备：

- 一个 PAS 管理员账号；
- 已注册 PolarRAG 实例，且目标 Space 已启用；以及
- 一个在文档所属企业中注册的身份提供商应用。

每个已验证身份源均可绑定多个已启用 Space。Space 的 `identity_domain` 来自 PolarRAG Space
目录，不是管理员输入，也不要求等于 Space ID。

## 配置 SharePoint 身份源

SharePoint 来源通过 Microsoft Graph 同步 Microsoft Entra 目录中的用户、用户组，以及用户或
嵌套用户组的直接成员关系。同一个 Entra 应用还为 PAS 用户提供免密码的 SharePoint 登录。

SharePoint 接入分为三步：先在 PAS 配置外部访问地址，再在 Microsoft Entra 注册并授权应用，最后回到
PAS 创建身份源。

### 第一步：在 PAS 配置外部访问基础 URL

PAS 是 **PolarDB Agentic Server**，即当前 MCPServer 的管理控制台和认证服务。若需要使用
SharePoint 登录，在 PAS 中进入**设置** > **配置** > **服务运行策略**，将**外部访问基础 URL**设置为
用户浏览器可访问的 HTTPS PAS 地址，执行检查并激活配置。例如：

```text
https://pas.example.com
```

仅测试目录同步时，PAS 会主动调用 Microsoft Graph，不需要配置重定向 URI。测试或上线 SharePoint 登录时，
该基础 URL 必须与下一步的 Entra 回调地址使用相同主机、协议和端口。

### 第二步：在 Microsoft Entra 注册并配置应用

先确认 Entra 租户所属环境：

| 环境 | 适用租户 | 应用注册门户 |
| --- | --- | --- |
| 全球云 | 全球 Azure / Microsoft 365 租户 | [Microsoft Entra 管理中心](https://entra.microsoft.com/) |
| 中国云（世纪互联） | 由世纪互联运营的 Microsoft 365 中国租户 | [Azure 中国门户](https://portal.azure.cn/) |

进入上一步对应门户中的**Microsoft Entra ID** > **应用注册** > **新建注册**，选择**仅此组织目录中的
帐户（单租户）**。创建后从**概述**
复制目录（租户）ID 和应用程序（客户端）ID。

然后进入**证书和密码** > **客户端密码** > **新客户端密码**，填写说明并选择有效期后创建。立即复制
新密码的**值**，并填入 PAS 身份源表单的**客户端密钥**；不要填写密码 ID。该值只显示一次，过期后需
创建新密码并在 PAS 中更新配置。

配置来源前，先进入 **设置** > **配置** > **服务运行策略**，将**外部访问基础 URL** 设置为
与下方登录回调相同主机、协议和端口的 PAS 地址，并激活配置。线上环境例如：

```text
https://pas.example.com
```

在 Entra 应用注册页进入**API 权限** > **添加权限** > **Microsoft Graph** > **应用程序权限**，添加以下
权限；随后点击**代表 <租户名称> 授予管理员同意**，确认两项均显示“已授予”。

| 权限 | PAS 使用原因 |
| --- | --- |
| `User.Read.All` | 枚举 Entra 用户及其稳定对象 ID。 |
| `GroupMember.Read.All` | 枚举用户组和直接成员关系。 |

仅测试目录同步时，注册页面中的**重定向 URI（可选）**可以留空；该同步由 PAS 主动调用
Microsoft Graph。只要测试**使用 SharePoint 登录**，包括线下测试，都必须进入**身份验证** >
**添加平台** > **Web**，填写完整的 HTTPS 回调地址：

```text
https://pas.example.com/auth/sharepoint/login/callback
```

如果应用已经创建，可进入**身份验证**并选择其 **Web** 平台。使用**添加 URI**新增另一个 PAS 回调地址，
或直接编辑已有地址进行替换；每次修改后保存平台配置。每条重定向 URI 必须唯一，已废弃的回调地址应删除，
不要重复添加相同地址。

基础 URL 与重定向 URI 必须和 PAS 的外部可访问主机、协议、端口完全一致；不能只填写 PAS 根地址。
Microsoft Entra 会在授权开始前拒绝非 localhost 的 HTTP 回调（`AADSTS500117`），即使“公共客户端/
本机（移动和桌面）”页面允许保存该地址也不能用于 PAS。

#### 线下测试与线上部署

线下若仅验证目录同步，不需要配置回调地址。线下若验证 SharePoint 登录，应先用受浏览器信任的证书通过
HTTPS 反向代理或测试域名暴露 PAS；然后将该 HTTPS 基础 URL 写入 PAS 和 Entra。线上部署使用相同的
**Web** 平台和 HTTPS 回调配置。

### 第三步：在 PAS 创建 SharePoint 身份源

在 **用户** > **企业身份源** 中选择 **新增来源** > **SharePoint**，选择与第二步相同的**全球云**或
**中国云（世纪互联）**，再填写从 Entra 获取的目录（租户）ID、应用程序（客户端）ID 和客户端密码的
**值**。创建后点击**强制同步**，直到状态显示为 `active`。

PAS 会针对所选云环境用这些凭证换取 app-only Graph Token，只保存加密后的来源配置和同步目录事实。
完整快照写入后来源才会显示为 `active`；刷新失败时会变为 `stale`，其主体将按 fail-closed 处理。

SharePoint 用户和用户组主体使用 Entra 对象 ID。文档导入链路也必须把相同对象 ID 写入
PolarRAG ACL；PAS 不会按邮箱、UPN、展示名或 SharePoint 站点本地组 ID 猜测映射。

来源变为 `active` 后，用户可在 PAS 登录页选择**使用 SharePoint 登录**。PAS 会使用 PKCE、
Entra 签名密钥、issuer、audience 和 nonce 校验授权码响应。Entra 对象 ID 声明（`oid`）是稳定的
PAS 企业用户 ID；首次登录会创建一个无密码用户。已有 PAS 用户也可通过**绑定企业身份**关联到
已同步的 SharePoint 身份。

## 配置飞书身份源

飞书接入分为两步：先在 PolarDB Agentic 中配置 PAS，再在飞书开放平台配置应用。

### 第一步：在 PolarDB Agentic 中配置 PAS

PAS 是 **PolarDB Agentic Server**，即当前 MCPServer 的管理控制台和认证服务。**外部访问
基础 URL** 使用当前 MCPServer 对用户公开的访问地址：从浏览器地址栏复制当前控制台地址，删除
`/login`、`/users` 等页面路径、查询参数和片段，只保留协议、主机和端口。例如：

```text
https://pas.example.com
```

登录 PAS 后，进入**设置** > **配置** > **服务运行策略**，填写该地址到**外部访问基础 URL**，
执行检查并激活配置。生产环境必须使用可被用户浏览器访问的 HTTPS 地址。

### 第二步：在飞书开放平台配置应用

在[飞书开放平台](https://open.feishu.cn/app)创建并发布飞书自建应用。在**网页应用**中，将
**桌面端主页**填为第一步的 PAS 基础 URL；再进入**安全设置** > **重定向 URL**，添加以下两条
精确回调地址：

```text
https://pas.example.com/auth/feishu/tenant-verification/callback
https://pas.example.com/auth/feishu/login/callback
```

`tenant-verification` 用于验证并激活企业身份源；`login` 用于个人用户免密码飞书登录。漏配
`login` 回调会导致飞书返回 `20029`。

基础 URL 和回调地址必须与 PAS 的外部可访问主机、协议和端口完全一致。不要将 callback
地址填入桌面端主页。生产环境回调地址必须使用 HTTPS。

在**发布管理** > **创建/修改版本** > **可用范围**中，将管理员本人、管理员所在部门或
**全部成员**加入可用范围。验证身份源和登录 PAS 的用户必须位于该应用的可用范围内；修改后
请发布版本。

在飞书应用的**开发配置** > **权限管理**中打开**通讯录**，点击**配置**并在**数据范围**选择
**全部成员**；随后为该范围开通以下**应用身份**权限：

| 权限 | PAS 使用原因 |
| --- | --- |
| `contact:contact.base:readonly` | 读取通讯录基础信息。 |
| `contact:department.base:readonly` | 读取部门树。 |
| `contact:user.base:readonly` | 读取租户用户和部门成员关系。 |
| `contact:user.employee_id:readonly` | 读取租户级 `user_id` ACL 标识。 |
| `contact:group:readonly` | 读取通讯录用户组及成员关系。 |

以上五项权限已满足 PAS 目录同步所需的最小权限范围。

文档 ETL 已展开文档 ACL 中的部门、通讯录用户组和群聊，并将规范用户成员关系持久化到
`acl_principal_membership`；可选 `acl_group` 也存储在同一快照。PAS 仅用该快照补齐目录 API
无法获得的主体；用户、部门和通讯录用户组直接从飞书同步。目录同步应用可以不同于文档导入应用，但两者必须属于同一飞书租户，且
都使用租户级 `user_id`。

### 创建、验证和绑定飞书来源

在 PAS 控制台打开**用户** > **企业身份源**：

1. 点击**新增身份源**，选择飞书，仅填写名称、App ID 和 App Secret。PAS 会立即跳转到
   飞书。
2. 登录并授权应用。PAS 消费一次性 state，获取已验证的 `tenant_key` 后返回控制台；不会
   保存管理员的用户 access token。
3. PAS 会直接从飞书同步用户、部门和通讯录用户组。控制台暂不展示可选的 ACL 成员快照配置。
4. 点击**绑定 Space**，多选需要使用该来源的已启用 PolarRAG Space 后确认。弹窗会同时列出
   已绑定 Space；点击某个 Space 旁的**解绑**即可移除。下一次 ACL 计算起，该来源主体不再用于
   这个 Space。

在租户验证完成且首次目录同步成功前，来源保持待配置状态，不会贡献 ACL 主体。来源绑定仅让
同步主体成为该 Space 的候选主体，本身不会授予任何文档访问权限。

如果租户验证没有完成，先确认已激活的**外部访问基础 URL**和飞书**重定向 URL**中的地址
与上述 callback 完全一致；然后再次点击**验证飞书租户**。每次验证都会使用新的单次
state。

如需通过自动化管理身份源、用户映射或 Space 绑定，请参见
[企业身份源管理员 API](../reference/enterprise-identity-sources-api.md)。密钥只能放在密钥管理服务或
受保护的运行环境，不能写入 URL 或代码仓库。

## 验证同步

PAS 会在五分钟内发起首次后台尝试，之后每五分钟重复一次。对于已就绪来源，可在来源列表点击
**立即同步**。管理员 API 的同步、状态查询和错误处理语义参见
[企业身份源管理员 API](../reference/enterprise-identity-sources-api.md)。

`active` 表示最新快照已完成。`stale` 表示最近一次刷新失败；在下次刷新成功前，PAS 不会把
该来源纳入新的 ACL context。`last_error` 只包含脱敏后的错误类型。请按时间在受保护的 PAS
日志中排查，不能记录或分享应用密钥、快照账号密码。

对一个测试用户，核对租户级 `user_id` 和预期主体 ID 是否与 PolarRAG 文档 ACL 完全一致。
PAS 会产生 `user`、`department`、`group` 和 `acl_group` 主体。主体 ID 不透明且区分大小写。
来源绑定和同步成功均不会绕过 PolarRAG 的 Space、知识库、owner 或文档 ACL 检查。

## 登录和编辑

目录同步会创建或更新无密码 PAS 用户。用户可在 PAS 登录页选择**使用飞书登录**；PAS 校验单次
授权响应与已验证租户一致，并在用户首次登录时自动创建账号。不要给已同步身份设置密码。

当某个 provider 仅有一个 `active` 身份源时，对应登录操作会直接跳转到该 provider。当同一
provider 存在多个 `active` 身份源时，PAS 会先展示来源选择页；用户选择企业身份源后再继续
provider 登录。PAS 将选中的来源绑定到一次性 state；回调不接受用户提供的来源或租户选择。

来源表可查看已同步用户和组。编辑飞书来源时需要再次输入 App ID 和 App Secret，并重新验证
租户；删除来源会撤销目录数据、Space 绑定和 Agent 组授权，但不会删除已存在的 PAS 用户。

编辑 SharePoint 来源时需要重新填写租户 ID、客户端 ID 和客户端密钥。已存在的 SharePoint 用户
会话保持有效，下一次登录会使用更新后的应用注册配置。

## 用户身份映射与 PolarRAG 访问

**用户**列表会在用户名下展示租户级外部 `user_id`，并在独立的**身份来源**列展示来源标签。**部门**
单元格会汇总本地 PAS 部门以及只读的企业部门和通讯录用户组。`PAS` 表示本地账号；飞书或
SharePoint 标签表示已同步的企业身份。组织信息为空表示该来源没有返回该用户的成员关系。

PAS 会在服务端构造的 `acl_context` 中始终附加该用户稳定的
`polarrag:user` 别名。同步用户的别名基于其 PAS external ID；管理员将该企业身份绑定到已有
PAS 用户后，PAS 还会在该身份源已绑定的 Space 内保留同步账户的来源原生别名。这样，原同步账户
已拥有的 Personal 知识库和文档仍可由绑定后的 PAS 用户访问，同时请求仍携带飞书或 SharePoint
用户和用户组主体。PAS 不会将任一别名投影到未绑定或已过期的身份源。

仅当需要把已有本地 PAS 账号关联到一个同步企业用户时，才使用**绑定企业身份**。这是一条由管理员
维护的映射，可编辑、可移除；相同页面还允许管理员为该 PAS 用户新增、停用或删除手工维护的 Feishu、
SharePoint 或原生 PolarRAG 主体。不要按邮箱或显示名称猜测映射，应从已同步身份中选择，以确保
PAS 使用稳定的外部用户 ID。

## Agent 群组访问

常规配置应在 Agent 已绑定 PolarRAG 实例后，从 Agent 详情页使用**配置企业访问**。该可预览
操作一次选择一个有效 Source、同步组或 PAS 用户以及可用 Space，并区分 Agent 局部授权、
缺失的全局 Source-Space 绑定和将复用的既有关系。创建缺失的 Source-Space 绑定会改变全局
共享状态。

PAS 每次 Agent 访问都会校验当前成员关系；用户离开群组、群组被禁用或来源变为过期状态后，
对应的群组派生 Agent 访问会立即失效。从 Agent 删除具体用户、组或全部同步用户授权，不会
删除全局 Source-Space 绑定、Agent-实例绑定或共享 PUBLIC 范围。

**企业身份源 · 全部同步用户** 是独立且可移除的显式授权项，展示在首位且绝不默认勾选。它仅在来源保持有效且未过期时，
覆盖该来源当前及后续同步的有效用户；不会包含无关的 PAS 用户，也不会绕过 PolarRAG 的文档
ACL。控制台支持多选用户或用户组；**全选用户组** 会刻意排除身份源全员项，因此来源全员授权
始终需要管理员明确选择。

既有身份源 Space 页面以及 Agent 用户/组控件仍保留，用于高级细粒度管理。
