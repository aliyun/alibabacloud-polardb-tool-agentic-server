# Agent 与 Token

[English](../../en/administration/agents-and-tokens.md)

控制台将员工归入**访问管理 → 个人账号**，Agent 归入**服务账户（原 Agent）**。通过**资源**管理数据库授权，通过**连接 MCP**直接以个人身份访问，无需 Agent。下文的既有高级配置和 Agent 连接继续支持。参见[账号与资源](accounts-and-resources.md)。

Agent 是非人类 MCP 身份，拥有独立的状态、Token、实例直连绑定、供应绑定和
自有资源。

PAS 提供两种相互独立的 Agent 相关凭证：

- `pas_agent_` 代表机器 Agent，可以获得数据库 Tool；
- `pas_user_agent_` 代表“一个明确分配的 PAS 用户 + 一个 Agent”，可以获得
  所选 Space 支持且授权允许的 PolarRAG 读取、管理和上传 Tool。

`pas_agent_` Token 不能冒充用户，也不能调用 PolarRAG Tool。

## 创建与连接

创建 Agent 时填写清晰的名称和用途。详情页展示有效 Token 状态、MCP 服务 URL，
并提供 Token 和 JSON 客户端配置的复制操作，其中 MCP Server 名称默认使用
Agent 名称。只复制到预期的客户端。

复制操作使用当前已认证管理员 Session 读取有效 Token，不会再次要求输入 PAS
密码；读取仍受审计和限流保护。控制台会把密钥直接写入剪贴板，在受控私网 HTTP
页面上也会使用降级复制，并且不会在页面中显示明文。应把 Agent 详情页和未锁定的
管理员 Session 视为密钥访问，不要把 Token 截入截图、工单或日志。

## Token 生命周期

重新生成会立即使旧 Token 失效；吊销会阻止认证，直到签发新 Token。停用
Agent 会独立于 Token 状态阻止新操作。已有 MCP Session 可能保留旧工具目录，
因此状态、Token 或绑定变化后应重新连接。

用户签发或重新生成 `pas_user_agent_` Token 时，`expires_at` 为可选项；留空会
创建没有固定过期时间的 Token。PAS 不增加 `issued_at`，也不会因空闲而使该 Token
过期；`last_used_at` 只用于遥测。显式到期、吊销、用户或 Agent 停用以及 Agent
分配关系撤销，都会在下一次认证请求立即生效。
显式 `expires_at` 必须带时区偏移。PAS 会在持久化前转换为 UTC；不带偏移的
datetime 会被拒绝。

## 访问绑定

直连绑定选择已注册实例、凭证、权限和能力。SQL 代理能力是可选项，可以开放
`sql:read`，并在 `readwrite` 下开放 `sql:write`。供应绑定只适用于健康的
`multitenant` 后端，可以在没有直连 SQL 权限时单独开放
`db_instance:create`。

已经绑定到 Agent 的实例不会再次出现在新绑定选择器中。需要调整时应删除或
编辑已有绑定，而不是创建重复绑定。
Agent 详情页按 **数据库实例** 和 **PolarRAG 实例** 两个页签管理。两个页签为
方便使用都会展示同一份 MCP 连接信息，底层仍共用同一个 Agent Token 和 MCP
端点。数据库绑定、REST 连接信息、供应路由和资源归入数据库页签；PolarRAG
实例绑定以及用户、组分配归入 PolarRAG 页签。

## PolarRAG 用户连接

Agent 详情页将 PolarRAG 访问配置编排为三个步骤：**绑定实例与 Space**、**配置组和用户**、
**绑定知识库**。第一步通过两个独立的服务端分页视图展示当前 Agent 的活跃且未绑定实例和
已绑定实例，并提供批量绑定与解绑操作。Agent 页面不再提供全局 Source-Space 关系的手工操作区；该关系由
企业身份源配置统一管理，仍可通过管理 API 查询或解绑。

Source-Space 关系是用户、组和知识库授权的父级。解除某个身份源与 Space 的关系时，会清理
该来源在此 Space 中的全部用户和企业组知识库授权；只要仍有其他身份源绑定，该其他来源和
PAS 用户的直接授权都会保留。解除 Space 的最后一个身份源时，会清理该 Space 的全部下级
知识库授权，避免重新绑定后历史授权意外恢复生效。

第二步配置当前 Agent 可使用的企业身份源、PAS 用户、Department 与用户组，并分为三个分页
页签。每个页签都可筛选已绑定或未绑定记录，支持搜索、全选或取消全选当前页，以及多选绑定和
解绑。身份源快照处于
stale 状态时，只有后端仍判定快照可用才允许操作。

Agent 和 KB 用户选择器在服务端完成可绑定候选过滤，再返回分页结果和搜索总数。
已停用的 PAS 账号不作为候选。企业用户需要在当前 Agent 启用 Space 所关联的可用身份源中
具有启用的目录身份；KB 选择器进一步限定到所选 KB 的 Space。没有目录记录时，未过期的
本地管理主体可提供有效身份，但不能覆盖已停用的目录记录。多个身份映射按提供方、租户、
外部主体和 PAS 用户 ID 判断，不按姓名过滤；其中一个身份停用不影响另一个范围内的有效身份。
PAS 自建账号及没有企业身份映射的普通 OIDC 账号不要求企业目录记录。active 身份源及成功
快照未过期的 stale 身份源仍可用。

已有绑定即使用户不再符合候选条件，也保留展示和解绑入口，并标注**已停用**或**身份不可用**。
用户选项响应增加 `availability=active|disabled|unavailable`，与绑定状态及 PAS 账号状态分开。
Agent 的 `binding_status=all` 返回可绑定候选与已有绑定的合集，`unbound` 仅返回可绑定的未绑定
用户。KB 的 `available` 排除不可绑定用户，`bound` 保留其已有 KB 绑定。该过滤不会修改账号状态、
删除身份或授权，也不会撤销已有 Token；这些属于独立的管理操作。

第二步的**绑定全部用户**将全部符合条件的企业身份源和启用的自建用户加入当前 Agent，
不受搜索和分页影响。身份源必须快照可用，且已绑定当前 Agent 已绑定实例中的启用 Space。
整个身份源的成员关系涵盖后续同步用户。PAS 自建用户按一条 Agent 动态策略整体授权，
不逐用户写入：后续新建且启用的自建用户自动加入；新增身份源仍需再次执行全绑。
升级保留已有逐人授权，需执行一次全绑以启用自建用户动态策略。
自建用户授权只更新一条策略，写入成本不随用户数增长；页面汇总数量由数据库计数。
**解绑全部用户**需确认，将移除当前 Agent 的全部用户、部门、用户组和企业身份源成员关系，
对应用户的 Agent Token 同时失效。不会修改账号、知识库、其他 Agent 或数据库实例授权。
现有 KB 用户策略保留；依赖组成员绑定的 KB 授权随该组绑定删除。
部分绑定状态下也可以执行全绑或全解绑，重复操作幂等；单项或批量操作后刷新按钮状态。
这里管理 Agent 成员关系，不是第三步的 KB 级 `select_all` 策略。

Agent 的有效授权是直接用户、部门、用户组、身份源全员和自建用户全员授权的并集。
移除直接绑定后，继承授权可能仍使用户可访问 Agent；解绑确认和结果会提示这一点，
已绑定列表显示保留的授权来源。知识库授权和文档 ACL 仍须另行满足。
迁移 `cf4a5b6c7d8e` 增加默认关闭的自建用户策略，不会从历史逐人授权推断动态全员授权。

管理员可通过 `GET /api/agents/{agent_id}/all-subject-assignments` 查询状态，
或向同一路径 `POST` `{"operation":"BIND"}` / `{"operation":"UNBIND"}` 修改成员关系。
两者均返回 `status`（`BOUND`、`PARTIAL`、`UNBOUND`）、`eligible_source_count`、
`eligible_user_count` 和 `assignment_count`，其中自建用户整体策略计为一条授权。
`BOUND` 要求已开启该策略且已绑定全部有效身份源；仅有历史逐人授权不会开启动态成员关系。
状态为 `BOUND` 或没有候选时不可执行全绑；
只有完全没有成员关系时才不可执行全解绑。旧版批量用户绑定任务仍运行时返回 `409`。
Agent 成员配置区并列展示**绑定全部用户**与**解绑全部用户**，按实际成员状态分别置灰，
加载或操作期间均禁用。公共/个人知识库可见范围也并列显示绑定、解绑：全部已绑定时禁用
绑定，未绑定时禁用解绑，部分绑定时两者均可用。KB 全部用户策略仍对未绑定目标只显示
绑定、全部已绑定目标只显示解绑，部分状态使用下拉组合按钮。KB 主体抽屉的**可绑定**页只提供**添加到所选 KB**，
**已绑定**页只提供**从所选 KB 移除**。加载状态中、没有选中项或已达到目标状态时禁用操作。

第三步统一列出具有 Source-Space 父级绑定的 Space 下的知识库，不按企业应用同步或自建分类。
旧的 `management_mode` 元数据不再筛选或限制知识库绑定操作。任意已绑定 KB 均可通过
**管理用户绑定关系**操作管理手工授权，抽屉标题显示知识库名称。手工授权和外部同步授权
分别维护：移除手工授权不会撤销仍有效的同步授权，下次同步只替换其自身授权。
**访问范围**显示“全部 Agent 用户”“已配置部分用户/组”或“未配置”，并注明继承授权来源。
勾选多个 KB 后才显示**批量管理绑定关系**，每个主体区分“全部选中 KB 均已绑定”“部分已绑定”
和“均未绑定”。**添加到所选 KB / 从所选 KB 移除**只增删所选关系，保留其他 KB、其他主体的
配置和 Agent 知识范围模式。全选下移除主体会记录排除项并保留全选；非全选状态下只移除直接手动绑定，其他继承授权需到来源处调整，
抽屉不会解绑 Agent 成员。“用户”“Department 与用户组”“企业身份源”三个页签均按所选 KB
提供**可绑定 / 已绑定**视图，包含尚未加入 Agent 的候选主体；添加绑定时会补齐 Agent 成员关系。
企业身份源及其目录组仍受有效身份源和身份源已绑定全部所选 Space 的限制。部分已绑定的主体在两侧均展示。
移除直接绑定后，主体重新出现在可绑定集合，除非继承授权仍覆盖全部所选 KB。
选择器使用后端游标分页（上一页/下一页），知识库列表同样由服务端分页，均支持全选当前页和取消选择。
切换页签或筛选时清空旧选择和操作提示。
**绑定全部用户**保存 KB 级 `select_all`，涵盖当前及后续加入 Agent 的成员，不枚举用户。
单独移除用户、Department、目录组或企业身份源时保留全选并记录排除项。排除项按当前组成员关系
动态生效，优先于手动授权。重新绑定同一主体只清除该主体自身的排除项；上级组或身份源的排除须
单独恢复。再次**绑定全部用户**清除全部排除项。**解绑全部用户**关闭所选 KB 的全选默认值并
清空其手动用户授权，不影响其他 KB、Agent 成员关系或外部同步授权。KB 级策略优先于 Agent
的 `LEGACY_ALL` 默认值。仅当全部所选 KB 均完整全选时禁用绑定全部；均无授权时禁用解绑全部；
部分状态下两个按钮均可用。操作幂等，完成后刷新按钮和主体列表，仍严格遵循 owner 与上游 ACL。

`GET /api/admin/agents/{agent_id}/knowledge-resource-options` 每行增加
`kb_type`、`agent_bound`、`access_scope`（`ALL_USERS`、`SELECTED` 或 `UNCONFIGURED`）及 `inheritance_sources`。
可选 `binding_status=bound|unbound` 在分页和统计总数之前筛选；不传时返回两个集合。
**绑定知识库**分为已绑定和未绑定视图，分别支持全选当前页、多选解绑或绑定自建 KB。
已绑定 KB 保留逐个及批量管理用户绑定关系入口。切换筛选或页码会清空选择；手工绑定操作不会修改外部同步授权。
`POST /api/admin/agents/{agent_id}/knowledge-binding-status` 返回生效的
`bound_subject_indexes`、可直接移除的 `direct_bound_subject_indexes`，以及按请求主体从零开始
的索引组织的 `inherited_subject_sources`。每个 `items` 条目在保留 `knowledge_resource_id` 的同时，
还返回 `space_id`、`kb_id`。资源 ID 和 Space/KB 两种请求选择器返回相同的标识，
原有状态字段与请求顺序保持不变。`ALL_AGENT_KNOWLEDGE` 表示继承 Agent 全部知识库默认
范围。状态查询不会创建 Agent 成员关系。Agent 全部知识库默认范围仅由 Agent 成员继承，
不代表所有尚未加入的候选主体都已绑定。
在 Agent 全部知识库默认授权或 KB 级全选策略下，目录子组也会通过同一身份源中已加入
当前 Agent 的有效祖先组继承成员资格。绑定状态及已绑定/可绑定筛选反映该继承关系，
不会额外创建子组的直接绑定。已有 KB 排除项继续生效，内容访问仍检查 owner 和上游 ACL。
排除项重叠状态计算忽略已停用的目录组，以及经过已停用祖先组的路径。
独立的有效路径和仍有效的外部主体授权继续生效。
`partial_subject_indexes` 表示含部分排除的组，`excluded_subject_indexes` 表示被排除的主体；
`SELECT_ALL` 与 `SELECT_ALL_EXCEPT` 标识 KB 级策略。

`POST /api/admin/agents/{agent_id}/knowledge-subject-options` 提供 KB 抽屉候选分页。
请求体包含 `knowledge_resource_ids`（1–100 个）、`category`（`users`、`groups`、`sources`）、
`binding_status`（`available` 或 `bound`）、可选 `search`、`offset`（初始为 0）和
`limit`（1–100，默认 20）。响应包含 `items`（每项有 `subject`、`label`、`identity_source`、
`status` 和 `inheritance_sources`）及 `next_offset`。下一页原样传入该游标，`null` 表示结束。
`available` 包含 UNBOUND/PARTIAL 主体，`bound` 包含 BOUND/PARTIAL 主体。此管理员接口只读。
用户候选在分页前批量筛选，接口不计算总数。翻页继续使用 `next_offset`，不要按返回条数
递增游标；改变搜索词或绑定筛选条件时从 offset 0 重新开始。抽屉对搜索输入做防抖，
并丢弃先前搜索或抽屉上下文的过期响应。这些优化不改变继承授权、全选排除项或外部同步授权优先级。
现有 `/api/agents/{agent_id}/user-options` 和 `group-options` 也支持 `binding_status=all`，
包含已加入和未加入 Agent 的候选项；默认的 `unbound` 行为保持不变。

预览会区分 Agent 局部授权、新增的全局 Source-Space 绑定以及已配置并复用的关系。确认缺失
的 Source-Space 绑定会改变全局共享状态。从当前 Agent 删除用户、组或全部同步用户授权时，
只删除该 Agent 授权；全局 Source-Space 绑定、Agent-实例绑定和共享 PUBLIC 范围都保留。
实例权限、PUBLIC 范围摘要及实例解绑统一位于第 1 步的**已绑定实例**页签，不再单独展示
“已绑定 PolarRAG 实例”表格。

Agent 列表、已分配/未分配用户和组选择器、PolarRAG 实例选择器、Space 以及 PUBLIC
知识资源选择器都使用后端搜索与分页；修改搜索词会回到第一页。即使租户包含大量
用户、组、Space 或知识库，Agent 详情页和企业访问弹窗也不会先加载全部对象。

管理员只能查看分配关系和 Token 状态，也可以强制吊销，但永远看不到用户 Token 明文。

被分配的用户登录后，在 **My Instances** 的 **MCP connections** 中签发、查看、
重新生成或吊销自己的 Token。复制操作使用当前已认证的 builtin 或 SSO Session，
不会再次要求输入密码；读取仍受本人归属校验、审计和限流保护。每个分配关系最多
有一个有效 Token。最终知识范围是 Agent 绑定实例、PAS 用户可见资源和 PolarRAG
文档 READ 判定的交集。

Agent 未指定创建策略时使用 `LEGACY_ALL` 模式。在 `SCOPED` 模式下，PAS 将所有命中的用户直属范围
和组范围取并集，再与 Agent 全局 PolarRAG 上限及用户可见资源取交集。手工范围与外部
同步范围可以并存；管理员可完整替换范围，以修改任意用户、组或知识库绑定，无需重新
签发 Token。PolarRAG 文档 ACL 仍是最终授权门禁。
企业身份源与 Space 的绑定不是既有本地用户或独立管理主体访问的前置条件。
`LEGACY_ALL` 保留 Agent 的实例和知识库范围上限，同时遵守显式 KB 策略及排除项。
来自企业身份源的 ACL 主体仍要求对应的身份源与 Space 绑定；解绑后不再包含该源贡献的主体。
Agent 详情页的**绑定知识库**步骤会展示这些关系。管理员可以在一次操作中把多个已分配
用户、Department 或身份源用户组绑定到多个 KB，也可以解绑任意所选子集；外部同步授权由同步维护，手工操作只修改独立的手工授权。

**3 绑定知识库**以 PUBLIC 和 PERSONAL 各自的**绑定全部 / 解绑全部**操作替代“配置 PUBLIC 范围”。
这些操作仅调整已绑定实例及符合父级约束的 Space 下 Agent 的知识库可见范围，保留单独的用户授权。
分页列表保留未绑定 KB，单独显示 Agent 到 KB 的绑定状态；不在 Agent 可见范围内时不能管理用户。
未绑定的自建 KB 行提供**绑定知识库**，解绑全部后也能单独恢复。该操作仅恢复当前 KB，
保留已有用户策略，并刷新列表和顶部批量状态；成功后即可**管理用户绑定关系**。
部分绑定时两种全部操作均可重复执行并保持幂等。“绑定全部”包含后续新增的符合条件的 KB。
现有 PUBLIC 限制在明确操作前保持不变。

PERSONAL 知识库仍只有 owner 可见，绑定到 Agent 不会向其他用户共享，也不改变个人 Token 的访问规则。
发现列表和所有资源型 MCP 工具均校验 Agent 可见范围、用户策略、所有权及文档 ACL。
变更在下一次 Tool 调用生效，无需重新签发 Token。

新增管理员接口：

- `POST /api/admin/agents/{agent_id}/knowledge-all-users-status` 接收
  `knowledge_resource_ids`（1–100 个），返回 `items`，每项包含
  `knowledge_resource_id`、`space_id`、`kb_id`、`select_all`、`excluded_subject_count` 和
  `status`（`BOUND`、`PARTIAL`、`UNBOUND`）。
- `POST /api/admin/agents/{agent_id}/knowledge-all-users` 接收同样的 IDs 及
  `operation: BIND | UNBIND`，原子、幂等地修改并返回最新状态。此接口不能修改外部同步 KB。
- `GET /api/admin/agents/{agent_id}/knowledge-visibility` 返回
  `instance_count` 及 `PUBLIC`、`PERSONAL` 各自的 `statuses`。
- `POST /api/admin/agents/{agent_id}/knowledge-visibility` 接收
  `kb_type: PUBLIC | PERSONAL` 和 `operation: BIND | UNBIND`，返回最新可见范围。
  不会额外绑定实例或 Space。
- `POST /api/admin/agents/{agent_id}/knowledge-visibility:bind` 接收
  `knowledge_resource_ids`（1–100 个），幂等地仅将这些自建 PUBLIC 或 PERSONAL KB
  加入 Agent 可见范围。所有目标必须活跃、启用且位于已绑定实例及符合父级约束的 Space 下，
  否则整批返回 `422`。返回相同的可见范围摘要，保留已有全选默认值、用户策略及 owner/ACL 校验。
- `POST /api/admin/agents/{agent_id}/knowledge-visibility:unbind` 接收相同请求体并采用
  相同校验，原子地仅从 Agent 可见范围移除所选 KB，重复请求幂等。保留用户策略、Agent 成员、
  其他 KB 和数据库权限。从全量默认范围单独解绑时，保留动态全选并记录所选 KB 的排除项。
  后续新增的自建和企业同步 KB 仍在可见范围内，但仍须满足 owner、用户绑定和上游 ACL。
  单独重新绑定仅移除所选排除项；绑定全部或解绑全部都会清空对应类型的排除项。
  显式允许列表仍保持显式，迁移时不会自动转换。
  排除项仅作用于对应实例和 KB 当前类型，用户侧列表和游标分页同样执行排除。
  目录同步改变 KB 类型后，重新绑定也会清除该 KB 遗留在旧类型中的排除项。

实例绑定响应新增 `public_excluded_resource_ids`；`public_knowledge_resource_ids` 为 null
表示全部 PUBLIC KB 减去这些排除项。通过现有 public-scope API 替换范围时，也会清空
PUBLIC 排除项。旧版单独解绑产生的显式列表无法与管理员主动配置的列表区分；需要恢复
动态全选时，先点击**绑定全部**，再重新解绑需要排除的 KB。

现有 `knowledge-bindings:batch` 在目标 KB 全选时记录或移除排除项，重复解绑不产生重复排除。

部署时须先执行 `pas database migrate` 并保留元数据备份，再启动新版本。旧版本无法执行
全选排除规则，不兼容升级后的授权数据；不得直接用旧二进制连接该元数据。

## 复查

定期复查闲置 Agent、最后使用时间、自有资源，以及 Audit Logs 中的 SQL 和
PolarRAG 页签。停用客户端或人员自动化前先吊销 Token。

### 创建时选择知识库策略

在**创建 Agent**中选择 **LEGACY_ALL** 或 **SCOPED**。默认 LEGACY_ALL；SCOPED 默认没有隐式用户 KB 授权。
选择 SCOPED 后可勾选**自动绑定外部知识库**，默认不勾选；切回 LEGACY_ALL 会清除勾选。
投影授权前还需要绑定 Agent 资源、身份源和 Space。创建后可在统一的**绑定知识库**页面
管理 SCOPED Agent 的自动绑定开关；已有 LEGACY_ALL Agent 的页面隐藏该设置。文档读取继续检查文档 ACL。

`POST /api/agents` 支持 `knowledge_scope_mode` 和 `external_knowledge_auto_bind_enabled`，
省略时保持 LEGACY_ALL/false。LEGACY_ALL 开启自动绑定或模式值非法时返回 422，不创建 Agent。
创建和详情响应返回这两个已保存的值。


### 在 SCOPED 与 LEGACY_ALL 之间切换

在**绑定知识库**页面点击**切换为 SCOPED**或**切换为 LEGACY_ALL**。
模式变化时清除该 Agent 的旧外部自动绑定范围，保留明确的用户／组织手工绑定。
SCOPED 不复制 LEGACY_ALL 的隐式访问范围；返回 LEGACY_ALL 时关闭自动绑定，
恢复全局范围内的默认访问，仍受已有手工覆盖策略限制。文档 ACL 和 Agent 使用权限继续生效。

进入 SCOPED 时可勾选**自动绑定外部知识库**。提交前使用 MCP 已导入权限记录重算自动绑定，
不连接上游权限数据库或飞书 API。模式、开关、清理和重算在同一事务中提交，失败时全部回滚；
读取请求看到已提交的旧状态或新状态。重算处理量随已导入授权和符合条件的 Agent 映射增长，
不会展开身份源的全部成员。授权数据量较大时请求可能较长，请等待完成，不要重复提交进行中的操作。

弹窗默认勾选**清除全部 Agent 用户策略（包括管理员手工配置的全员策略）**。
请审核此选项：这些策略没有来源标记，清除时也会删除手工配置的全员策略及其排除项。
取消勾选可保留；保留策略可能导致自动权限撤销后知识库仍可见。
无论是否勾选，明确的用户／组织手工绑定均保留。已有 SCOPED Agent 可点击
**重新应用 SCOPED 权限**清理历史全员策略并重算外部绑定，不必往返切换两次。

仅管理员可调用 `PUT /api/admin/agents/{agent_id}/knowledge-scope-mode`，接受 `mode`、
可选的 `expected_mode`、`external_knowledge_auto_bind_enabled` 和
`reset_all_users_policies`（API 默认 false）。预期模式已变化时返回 409；
LEGACY_ALL 且请求开启自动绑定或重置策略时返回 422。


## 刷新外部知识库权限

管理员可以一次触发上游权限快照拉取和 Agent 绑定更新。Agent 必须采用 `SCOPED`、开启外部知识库自动绑定，且选中的有效知识库须位于 Agent 全局资源范围内。

```http
POST /api/admin/agents/{agent_id}/knowledge-permissions/refresh
Authorization: Bearer <管理员登录 Token>
Content-Type: application/json

{
  "identity_source_ids": ["<企业身份源 ID>"],
  "knowledge_resource_ids": ["<PAS 知识库资源 ID>"]
}
```

两个列表均可省略，但传入时不能为空。传 `{}` 时，根据 Agent 全局范围内的有效知识库，以及已绑定对应 Space、配置完整且可用的飞书身份源确定范围，不依赖已有自动绑定，因此可以发现新增授权。显式指定的每个知识库必须匹配至少一个选定身份源，每个显式指定的身份源必须有对应目标。未显式选择知识库时，跳过没有匹配权限来源的自建知识库。`identity_source_ids` 是 PAS 企业身份源 ID，不是飞书应用 ID；`knowledge_resource_ids` 是 PAS 内部知识库资源 ID，不是上游 `pkb-*` ID。

接口默认返回 `202`，包含 `job_id`、选定 ID、`resources_total` 和进度。
明确指定一个知识库时，可以设置 `wait_timeout_seconds`（0–30 秒，默认 0）：

```json
{"knowledge_resource_ids": ["<PAS-知识库资源-ID>"], "wait_timeout_seconds": 10}
```

等待模式要求明确指定且去重后恰好一个资源 ID，否则返回 `422`。
等待期间完成则返回 `200` 和 `SUCCEEDED`；进入 `FAILED` 或 `PARTIAL` 终态则返回
`409`（配置冲突）、`413`（授权超限）或 `502`（其他失败）、任务状态和脱敏错误。超过等待时间返回 `202`、当前进度和原 `job_id`，
后台继续执行。等待超时或客户端断开不会取消持久化任务；服务关闭时保留恢复检查点。
此操作拉取最新已生成的权限快照，不主动触发上游权限采集。通过管理员接口查询，直到任务进入终态：

```http
GET /api/admin/agents/{agent_id}/knowledge-permissions/refresh/{job_id}
Authorization: Bearer <管理员登录 Token>
```

状态包括 `QUEUED`、`RUNNING`、`SUCCEEDED`、`PARTIAL`、`FAILED`。`SUCCEEDED` 表示所有选定知识库均已处理，包含跳过项；`PARTIAL` 表示前面的 KB 已提交、后续 KB 失败。通过 `completed`、`current_knowledge_resource_id` 和脱敏 `error` 判断范围；完成项记录快照 generation、授权数量、读取时间和提交前的发布时间，这些时间不是延迟 SLA。同一 Agent 已有活动任务时返回 `409` 和原任务 ID。

快照 generation 和 fingerprint 未变化时复用共享授权行，仍根据当前访问配置重新计算所选 Agent 的绑定。

任务先读取一个 KB 在所有选定来源下的完整快照，再写入 PAS。该 KB 的多个权限来源、自动绑定替换、过期 Scope 清理和进度检查点一起提交。手工绑定、其他 KB 和其他 Agent 的绑定行保留。共享快照目录会更新，其他 Agent 后续刷新可能使用最新快照。多个 KB 分别发布，不保证整个 Agent 的全部 KB 同时切换。

快照读取失败、缺失、过旧、不一致、超时或超限时，不清除该 KB 之前已提交的绑定；原有快照有效期规则继续生效。发布前重新校验来源配置、来源与 Space 的绑定、Agent 模式、自动绑定开关和全局范围。刷新不会修改身份源目录生命周期状态或同步成功时间。

任务与逐 KB 检查点持久化在 PAS 元数据中。进程中断后，180 秒租约到期时继续未完成的 KB。每个 KB 的读取及发布限时 120 秒，所选来源合计最多 200,000 条授权；每个服务进程最多两个刷新 worker。超过边界会报告失败，不以清空现有权限处理。本操作读取已配置的权限元数据库，不强制触发飞书权限采集、Search 文档 ACL 重建或人员／组织目录同步。原身份源刷新和 Agent 已导入快照重算接口继续保留。

## 管理员查询完整知识库访问范围

```http
GET /api/admin/identity-sources/{identity_source_id}/agents/{agent_id}/users/{external_user_id}/knowledge-resources?access_origin=ALL&limit=200
Authorization: Bearer <管理员登录令牌>
```

`external_user_id` 为该身份源记录的用户标识（例如工号）。`ALL` 模式下，身份源仅用于
将此标识映射到 PAS 用户，不限制授权来源。接口复用 Agent 有效知识库范围计算，
合并用户／组织手工绑定、各身份源自动同步、全部 Agent 用户策略和 `LEGACY_ALL` 默认范围。
仍检查 Agent 准入、用户与 Agent 有效状态、全局范围、策略排除及资源可见性。
这是知识库级访问范围；检索和读取仍受文档 ACL 过滤，返回知识库不代表能读取全部文档。

默认 `access_origin=ALL`。使用 `access_origin=EXTERNAL_SYNC` 仅查询指定身份源的自动同步权限。
使用 `access_origin=MANUAL` 查询显式手工用户／组织绑定和全部 Agent 用户策略，应用手工排除规则；
不包含自动同步或 `LEGACY_ALL` 隐式默认范围。同一 KB 同时具有手工和同步授权时，
若手工授权有效，仍出现在 `MANUAL` 结果中。
移除 `FEISHU`，传入时返回 `422`；不传参数时改为返回完整有效权限。
响应保留 `items` 和 `next_cursor`，需持续分页直到游标为空。未找到用户映射或用户无 Agent
准入权限时返回空页；身份源／Agent 不存在返回 `404`，来源参数或游标无效返回 `422`。
接口只读，不触发权限刷新或签发用户令牌。
`/me/agents/{agent_id}/knowledge-resources` 仍仅查询自动同步权限。

### 在控制台刷新权限

对于已开启外部自动绑定的 SCOPED Agent，进入绑定知识库页面，点击**刷新当前 Agent 全部知识库权限**，
拉取最新已生成的权限快照并更新 Agent 全局范围内的自动绑定。
每个已绑定知识库行提供**刷新权限**，仅刷新当前 KB。顶部**刷新所选知识库权限**
同时支持勾选多个 KB；单库请求最多等待 10 秒，未完成则后台继续；多库及整个 Agent 刷新直接后台执行。

页面展示任务 ID、已完成／总知识库数量，并轮询直到结束。成功或部分成功后重新加载绑定列表。
失败知识库保留原绑定，部分成功明确提示未完成工作。进度查询失败时点击重试，仅重试查询，
不会启动新任务。任务 ID 保存在当前浏览器会话中，返回页面时恢复进度查询；重复任务响应会
跟进现有任务。离开页面不会取消服务端任务。页面跟进活动任务期间禁用模式切换、自动绑定
开关和已有快照重算操作。

次级入口**基于已导入权限重算**仅使用 PAS 已导入快照，不拉取最新快照。
权限刷新不会导入文档或主动触发上游权限采集。

单 KB、多 KB 或整个 Agent 刷新时，若某 KB 在权限数据源中没有快照，且所有所选来源均无已导入快照
或派生权限范围，则跳过该 KB，原绑定不变。进度统计已处理 KB（包含跳过项）；
`resources_skipped` 返回跳过数量，`completed` 中标明跳过的 KB 和原因。
已有导入或派生权限却缺失快照时仍报错并保留绑定。单库安全跳过也返回 `SUCCEEDED`、
`resources_skipped=1` 和跳过原因；等待期间完成返回 HTTP 200，否则返回 202 后查询任务。
有效的零授权快照正常处理，不作为跳过项。

### 刷新错误与任务保留

等待模式遇到 Agent、资源或来源配置冲突时返回 HTTP 409，授权容量超限返回 413。
其他终态刷新失败返回 502 和任务对象。网关错误可能返回非任务响应；客户端必须
校验任务 ID、Agent ID、状态和进度后再跟进。高并发下排队可能超过等待窗口并返回
202；等待查询逐步退避至一秒。

批量处理遇到首个实际错误即停止。当前失败 KB 和后续未处理 KB 保留原绑定，
`resources_unprocessed` 包含这两类；此前已发布更新仍生效。跳过项计入已处理但不算已发布。
接受新任务时仅保留该 Agent 的最新任务，被替换的终态任务查询返回 404；活动任务不能被替换。

`EXTERNAL_SYNC` 返回有效的同步访问范围，检查用户／Agent 有效状态、Agent 准入、
全局排除、个人知识库选择及资源可见性。行内刷新在完成或失败时也显示通知。
若所有目标均因无权限快照而跳过，汇总和行内通知显示警告，明确本次未应用更新；
接口状态仍为 `SUCCEEDED`。有效零授权快照仍属于实际应用的更新。

快照缺失表示状态未知，不代表撤权。失败时保留已提交绑定；撤权须用完整的零授权
或 DENY_ALL 快照表达。文档访问仍需通过上游 ACL 授权。

权限刷新后的列表重载保留当前页记录，读取成功后更新；读取失败显示错误，并明确
下方为刷新前的数据，提示管理员重试后再进行后续操作。
切换 Agent、筛选、搜索或分页时仍清除上一查询上下文，避免显示不属于当前范围的记录。

### 使用上游知识库 ID 刷新

```http
POST /api/admin/agents/{agent_id}/knowledge-permissions/refresh
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "kb_ids": ["pkb-example"],
  "space_id": "pks-example",
  "polarrag_instance_id": "<PAS-instance-ID>",
  "wait_timeout_seconds": 10
}
```

`kb_ids` 接受精确的上游 PKB ID（1–500 个非空值），与 `knowledge_resource_ids` 二选一。
`space_id` 为上游 Space ID，`polarrag_instance_id` 为 PAS 注册实例的内部 UUID。
实例／Space 可选且只能与 `kb_ids` 搭配，对整个列表生效；只能缩小 Agent 范围，不能扩大授权。
身份源筛选选择权限来源，不用于消除资源身份歧义。

每个去重后的 PKB 必须在应用限定条件后的 Agent 全局范围内，唯一匹配一个有效且启用的资源。
无匹配返回 422 和 `KNOWLEDGE_RESOURCE_NOT_FOUND`；多条匹配返回 422 和
`KNOWLEDGE_RESOURCE_AMBIGUOUS`。任意项解析失败时整次拒绝，不创建部分刷新任务。
重复 ID 去重；不能单独传实例／Space；`{}` 保持全 Agent 刷新，原内部 ID 调用不变。

无匹配错误回显应用的 `space_id` 和 `polarrag_instance_id`，限定条件不匹配并不一定
表示资源不在 Agent 范围内。若 PKB 已解析，但所在 Space 没有所选的已配置身份源，
返回 422、`KNOWLEDGE_RESOURCE_NO_CONFIGURED_IDENTITY_SOURCE`，以及对应
`kb_id`、`space_id`、`polarrag_instance_id`；不创建部分任务。

解析后只有一个资源支持限时等待，多资源异步执行。任务保存已解析的内部资源、实例、
Space 和 PKB 标识；发布时检查这些标识，重启恢复不会重新解析并改选其他资源。
权限快照缺失仍沿用当前单库／多库规则。
