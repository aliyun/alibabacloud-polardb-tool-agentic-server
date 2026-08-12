# 自动供给池（AgenticDB Dedicated）

[English](../../en/database-instances/dedicated-hot-pools.md)

自动供给池保存已验证、可供 Agent 快速分配的 PolarDB MySQL 集群。这是 PAS
唯一的资源池模型，不再存在独立的“旧版”或“用户资源池”。管理员注册的实例继续在
实例清单中独立分配。

## 供给模型

PAS 只支持以下三种数据库供给方式：

1. 管理员注册 `multitenant` 多租实例，配置高权限供应后端，再绑定给 Agent；
   Agent 可以自动创建逻辑租户/数据库资源。
2. 管理员注册 `single-tenant` 单租实例，把直连凭证分配给 Agent；Agent 直接使用
   该物理集群，PAS 不会为它购买或补充容量。
3. 管理员创建一个或多个自动供给池并配置目标容量；PAS 使用有效的阿里云
   身份购买 `AgenticDBType=dedicated` 集群，并把每个已购买集群作为所选自动供给池的
   成员统一管理。

人类 User 只能使用管理员分配的已注册实例，不会领取自动供给池成员或触发物理购买。
没有分配注册实例时，请求返回 `NO_INSTANCE_ASSIGNED`，并提示联系管理员。

## 启用和创建自动供给池

启用 Worker 前，先在每个环境执行增量元数据迁移。在 **服务配置 → 服务运行策略**
中设置 `dedicated_pool_enabled=true`。每个 PAS 副本都会轮询有效配置，由进程内
监督任务在线启动自动供给任务，不需要重启或滚动 PAS。激活或轮换阿里云凭证时，
监督任务也会在线重建 Worker 使用的云客户端。值为 false 时，multitenant Worker
继续运行，但自动供给 Worker 不会购买、预备、复检、同步权限、断开、恢复、清理或
销毁成员。

打开 **Pool** 创建自动供给池。页面可以存在多个自动供给池，由 Agent 路由决定每次
创建请求使用哪个自动供给池。需要配置：

- `target_size`：PAS 尝试维持的最低 planning 容量。
- `max_total_members`：计费成员硬上限，不能小于 `target_size`。
- `max_member_purchases_per_hour`：持久化、多副本安全的购买预算。
- `max_create_requests_per_agent_per_hour` 和
  `max_delete_requests_per_agent_per_hour`：仅 Dedicated 使用的 Agent 操作预算。
- 地域、可用区、VPC、VSwitch 和存储类型。这些标识需要手工填写；PAS 不会为了
  枚举网络资源而额外要求 RAM 权限。填写地域后，向导会链接到
  `https://vpc.console.aliyun.com/vpc/<region_id>/vpcs`，管理员可从阿里云 VPC
  控制台复制 VPC 和 VSwitch。VSwitch 必须属于该 VPC 和可用区，且 PAS 必须能
  访问该私网。
- 可选的 PAS 私网来源 CIDR，用作 PolarDB 白名单。这里填写未来执行授权和验证的
  PAS ECS 或 Kubernetes 部署的私网来源网段。不得为了本地开发方便填写
  `0.0.0.0/0` 或本地工作站的公网地址。
- `destroy` 或 `sanitize_and_reuse` 回收策略。
- 权限模板 Revision，以及生成数据库/账号名称的模板。
- 可选的自动供给池级 `delete_cooldown_duration_hours`。
- Available 健康检查间隔和最大证据年龄。最大证据年龄必须至少是间隔的两倍；
  默认值分别为 300 和 600 秒。

开始时使用 `destroy`。只有在当前部署已经验证完整的断连、清理、重新预备和权限
校验路径后，才启用 `sanitize_and_reuse`。

购买规格不再是自由填写的 JSON。PAS 只接受服务端内置的
`agentic-dedicated-mysql` 模板，并由服务端生成 `CreateDBCluster` 请求。Revision 1
固定使用 PolarDB MySQL `DBMinorVersion=8.0.2`、AgileServerless 计算模式、预创建的
`agentic` 数据库/账号及相关 Agentic 参数。用户可调整的维度是地域、可用区、VPC、
VSwitch 和服务端允许的存储类型。当前默认且唯一支持的存储类型为 `essdpl1`；选择器
允许未来的模板 Revision 增加物理磁盘类型，而不暴露任意购买字段。

权限选择器名称为**为 Agent 分配的默认 MySQL 权限**：它控制 PAS 为 Agent 分配
时返回的 MySQL 账号权限，不假设调用方一定是 Sandbox。

创建后，管理员可以从自动供给池详情页修改名称、容量和限流、存储类型、回收策略、
删除冷却时间及健康检查设置。地域、可用区、VPC 和 VSwitch 可在默认折叠的
**高级网络配置**中修改。PAS 在保存以及传给 `CreateDBCluster` 前会去除这些标识符
首尾的空白字符。

修改网络位置时，管理员必须显式确认风险，且请求必须携带最新的资源池配置 Revision。
该操作不会迁移已有 PolarDB 集群，因此资源池可能暂时同时包含旧网络和新网络中的
成员。管理员必须确认 PAS 以及路由到该资源池的 Agent 都能访问两处网络。尚未发起
云端购买的成员（包括进度仍为 `PURCHASE_INTENT_STORED` 的失败成员）重试时使用最新
保存的网络配置；已经进入 `PURCHASE_REQUESTED` 或后续步骤的成员继续使用已发送给
阿里云的网络位置。因此，正在进行中的请求仍可能按照修改前的配置完成。

PAS 私网来源 CIDR 会在购买新成员时传入。修改该配置只影响后续成员，不会自动协调
已有 PolarDB 集群的白名单。

权限模板 Revision 不可原地修改。管理员应在资源池权限面板中基于当前版本创建新
Revision，再决定是否将其设为未来 Agent 账号的默认权限。存量账号只有在单独执行
“预览并应用”后才会变化；资源池尚无 Agent 账号时，页面会明确说明无需同步。

## 运行就绪与模拟模式

向导第一步展示三个用户可处理的检查项：自动供给 Worker 心跳、阿里云购买凭证和
为 Agent 分配的默认 MySQL 权限 Revision。
未配置阿里云访问时返回 `ALIYUN_ACCESS_NOT_CONFIGURED`；没有最新 Worker 心跳时返回
`DEDICATED_WORKER_NOT_RUNNING`。管理员仍可保存自动供给池，但在全部阻塞原因消除前，其
状态保持“未启动”，不会生成实例。这也解释了本地环境中已经存在自动供给池元数据，但没有
配置 AccessKey、AssumeRole 或 ECS RAM Role 时没有实例的情况。

在线激活后，到 Worker 写入第一条持久化心跳之间可能存在短暂间隔；期间就绪状态仍为
`DEDICATED_WORKER_NOT_RUNNING`，但不需要重启 PAS。

Worker 检查项会说明 PAS 后台 Worker 负责购买、初始化、健康检查、回收和删除资源池
拥有的实例；阻塞时跳转到 `/settings/configuration?module=runtime_policy`。凭证检查项
跳转到 `/settings/configuration?module=aliyun_access`。已经就绪的检查项不显示重复
操作。

服务端购买 profile 仍会在后端执行完整性校验，但不是用户可编辑的检查项。
`PURCHASE_PROFILE_INVALID` 表示需要升级或修复 PAS 版本/安装；
`PERMISSION_TEMPLATE_UNAVAILABLE` 表示安装或元数据迁移不完整。两者都不能通过在
向导中填写任意购买参数解决。

`dedicated_pool_simulation_enabled` 是显式的开发/测试回退开关，仅在没有有效阿里云
凭证时生效。有效的 AccessKey、AssumeRole 或 ECS RAM Role 始终优先，控制台会报告
实际使用的真实模式，不再显示模拟警告。关闭该回退开关后，缺少云凭证会 fail closed。
生产环境不得保留该回退开关，也不能把模拟就绪当作 PolarDB 购买权限已经验证的证据。

`dedicated_pool_preparation_mode` 默认为 `full`。在 `full` 模式下，PAS 必须连接
PolarDB 私网 Endpoint、为 Agent 账号应用权限模板，并验证授权与 `SELECT 1`，成员
才能进入 `AVAILABLE`。本地工作站无法路由到该私网时，可显式选择
`openapi_only`：PAS 仍会创建真实且计费的集群、生命周期账号、Agent 账号和数据库，
然后持久化 `OPENAPI_READY` 并暂停。成员继续保持 `REPLENISHING`，不可分配，也不会
返回凭证。

PAS 不会根据 localhost、Docker 或主机名自动判断此模式。将 PAS 部署到能够访问已
保存私网 Endpoint 的 VPC 或互联私网后，把有效的服务运行策略切回 `full`；进程内
Worker 会从同一成员的 `OPENAPI_READY` 继续，不会重复购买集群。因此完整真实 E2E
必须在目标 VPC 或互联私网中运行 PAS。PAS 不会自动创建公网地址，也不会自动放宽
白名单。

管理员可以手工重试处于 `REPLENISHING` 的成员。该操作会清除计划退避并立即唤醒
进程内 Worker，从成员已持久化的准备步骤继续，绝不会发起第二次购买。成员在
`openapi_only` 模式下到达 `OPENAPI_READY` 时不会提供重试，因为它已经到达本地验证
所配置的暂停边界。将 PAS 部署到具备私网连通性的环境并切回 `full` 后，如果之前的
失败或退避仍阻止继续，可再执行重试。

## 生命周期管理员凭证

PAS 购买的成员使用 `pas_managed`。PAS 创建生命周期管理员，加密其用户名和密码，
并且只用于数据库/账号预备、权限变更、终止连接、验证和清理。该凭证永不返回给
Agent。

PolarDB `CreateAccount` 返回时，新账号不一定已经可查询。PAS 会通过
`DescribeAccounts` 分别等待生命周期管理员和 Agent 账号进入 `Available`，之后才
使用相应账号。创建数据库时不关联任何账号：PolarDB 高权限生命周期账号天然可访问
所有数据库，而 `CreateDatabase` 只允许关联普通账号。之后 PAS 连接数据面，按照所选
权限 Revision 为 Agent 账号应用精确权限。

向自动供给池注册外部成员时使用 `admin_provided`，并选择该实例有效的
`provisioning_admin` 凭证，Capability 必须为 `admin`。PAS 不会创建该管理员。
如果外部服务使用 PolarDB 多租户管理，该账号还必须满足其高权限要求，包括
`rds_kill_user_list` 前提。

管理员直接分配给 Agent 的普通已注册单租户实例，不是自动供应的自动供给池
成员，因此不要求生命周期管理员。它继续使用管理员分配和直连凭证管理。

## 预备与分配

新成员依次完成购买意图、集群创建、Endpoint 解析、生命周期账号就绪、Agent
数据库/账号创建、权限应用和验证。成员成为 `AVAILABLE` 前，PAS 已预创建数据库和
面向 Agent 的账号。

阿里云操作失败时，成员记录会保留有长度限制的请求 ID、失败时间、OpenAPI 接口、
安全化的云端错误详情和稳定的 PAS 失败码。鼠标悬停在请求 ID 上即可查看这些诊断
信息。疑似包含凭证的云端消息不会被持久化或展示。

只有证据年龄小于配置上限、同时处于 `AVAILABLE` 和 `FRESH` 的成员可以分配。
热创建会原子预留成员，并直接返回 `READY` 凭证；请求路径中不包含 PolarDB OpenAPI
购买或初始化 SQL。没有新鲜成员时，PAS 会先在所选自动供给池内创建
`ALLOCATED_PREPARING` 成员并固定到请求资源，然后才在相同硬限制下发起云端购买，
请求返回 `CREATING`。冷创建的集群不会游离于自动供给池计量之外，购买意图持久化后也不会
切换自动供给池。

自动供给池容量字段含义如下：

- `allocatable`：当前可用的新鲜已验证成员。
- `planning`：未分配的 `AVAILABLE` 成员，加上已经 `REPLENISHING` 的未分配成员。
- `billable_total`：所有非 `DELETED` 成员，包括已分配、`DELETING` 和冷却资源。
- `surplus`：超过 `target_size` 的 planning 容量；只有该值大于零时控制台才告警。

冷却成员不计入 `planning`，因此 PAS 可以补足自动供给池最低容量；但它们继续计入
`billable_total`。这可以阻止创建/删除循环在旧集群仍计费时绕过
`max_total_members`。

控制台会把自动供给池配置状态与派生的供给状态分开显示。供给状态包括 `NOT_STARTED`、
`PREWARMING`、`PARTIALLY_READY`、`READY`、`CAPACITY_LIMITED` 和 `ERROR`，同时显示
准确的 `N/M` 新鲜就绪容量及全部当前阻塞码。`POOL_CAPACITY_LIMIT_REACHED` 表示
计费硬上限阻止继续补充；冷却成员仍计入该计费数量。

## Agent 主池与回退路由

自动供给池可以在未绑定 Agent 时预热，但管理员把它加入 Agent 的 Dedicated 路由列表前，
没有 Agent 可以使用其容量。每个 Agent 有一个 `routing_order=0` 的已启用 **primary**
主池，以及零个或多个有序 **fallback** 回退池；不存在全局自动供给池优先级。Agent 详情页
可以追加回退池、确认后替换主池、调整回退顺序、暂停路由、解绑或打开自动供给池。

选择过程按 `routing_order` 依次查找新鲜热容量。没有热成员时先从主池发起冷购买；
只有在购买前已确认是确定性阻塞时，才允许回退池冷购买。保存购买意图后发生超时或
云端瞬时失败时不会跳到其他自动供给池，以防重复购买计费集群。调整顺序只影响未来创建；
存量资源仍固定在原自动供给池，并由原自动供给池完成删除、冷却、恢复和清理。存在非终态资源的
路由只能暂停，资源完成前不能解绑。

## 就绪与失败处理

PAS 定期复检 `AVAILABLE` 成员。就绪证据使用 `FRESH`、`STALE` 和 `CHECKING`。
证据过期会把 `FRESH` 改为 `STALE`，将成员排除在分配之外，并强制复检。仅证据
过期不会隔离成员或购买替代容量，从而避免健康 Worker 故障把原本健康的整个自动供给池
替换掉。

复检成功后成员恢复为 `FRESH`。内部或连通性故障导致的不确定结果会保持 stale，
并计划再次复检。只有结论明确的验证失败才把成员转为 `QUARANTINED`；补充容量仍受
成员硬上限和购买预算约束。管理员可以在自动供给池页面重试、显式隔离或销毁未分配成员。

使用 `POOL_CAPACITY_LIMIT_REACHED` 识别计费硬上限，使用 `RATE_LIMITED` 识别购买
或 Agent 操作预算。应针对持续 planning 缺口、补购速度、重复失败、stale/checking
增长、quarantined 增长和权限验证失败告警。

## 权限模板与同步

每个自动供给池固定到不可变的权限模板 Revision。默认模板包含常用数据库级 DDL/DML
权限，不包含 `CREATE USER`，并且 `grant_option` 为 false。管理员可以创建显式选择
受支持权限的新 Revision。`CREATE USER` 和 `grant_option` 是能力较强的 Dedicated
专属选项，默认永不启用。

修改自动供给池选中的 Revision 会影响后续预备。要修改已有成员或一个已分配资源，打开
权限抽屉，执行 `dry_run`，复查每个目标和原 Revision，然后显式确认 `apply`。PAS
保存持久化 Job，撤销旧有效权限，应用精确 Snapshot，并验证结果。失败目标保留脱敏
错误码，诊断时不会暴露凭证。

## 存量购买模板升级

购买 JSON 为空、不完整或与固定参数冲突的存量自动供给池会报告
`PURCHASE_PROFILE_UPGRADE_REQUIRED`，并禁止购买新容量。自动供给池详情页展示类型化替换
内容，包括 `DBMinorVersion=8.0.2` 和 `essdpl1`，并要求基于配置 Revision 显式确认。
升级会保留网络、容量、权限、冷却、成员和 Agent 绑定数据；PAS 不会静默解释或复用
任意存量 JSON。

## 已退役资源池兼容保护

原 `resource_pool` 配置模块、`/api/pool` 与 `/api/quota` 路由、人类用户自动购买
路径、retry-provision 端点及旧启动恢复 Sweep 均已退役。PAS 不会自动接管、删除或
静默转换旧物理记录。

Dedicated 购买前，PAS 会审计保留的元数据。非零旧目标容量返回
`LEGACY_POOL_CONFIG_PRESENT`；旧 `pooled` 物理记录返回
`LEGACY_POOL_INSTANCE_PRESENT`。任一码都会阻止新购买，管理员必须先备份元数据并
显式处理废弃状态。仅存在目标容量为零的旧配置文档不会创建新自动供给池或购买集群。

## 删除、冷却与恢复

删除先撤销 Agent 访问，然后由生命周期管理员终止 Sandbox 数据库账号现有的 MySQL
Session。PAS 只有在确认断开后才设置 `disconnected_at`；`cooldown_until` 等于该
时间加有效时长。断开尚未确认时，即使截止时间已过也不能开始清理。

时长继承顺序是成员/资源、自动供给池、全局 `delete_cooldown_duration_hours`。全局默认
24 小时。值使用整小时，且有意设置最小值 1；测试和运维演练通过注入时钟完成，
而不是降低生产下限。

不可逆清理开始前，管理员可从 `DELETING`、`DELETE_FAILED` 或 `COOLING_DOWN`
恢复。PAS 重新连接并验证旧数据库/账号，然后重新启用旧凭证，使无需修改的 Sandbox
继续运行。恢复失败会停留在可恢复来源状态，保持原截止时间，并记录脱敏原因。逻辑
清理、sanitize dispatch、物理销毁或完成后不能恢复。

到期后，`destroy` 删除物理成员。`sanitize_and_reuse` 删除 Sandbox 数据库/账号，
清除其凭证材料和权限 Snapshot，并让成员重新进入预备。两条路径都只释放一次活跃
容量。

## 排空与安全运维

排空会设置 `target_size=0`，并停止通过该自动供给池进行新分配；已有资源的断连、冷却、
恢复、权限同步和清理仍可完成。在所有关联成员和资源进入安全终态前，不要删除元数据
或撤销生命周期管理员。

将元数据库与 `PAS_ENCRYPTION_KEY` 作为一套恢复集备份。数据库保存加密的生命周期
和 Sandbox 凭证，缺少任一部分都无法恢复。审计记录覆盖自动供给池变更、成员动作、模板
Revision、同步和资源恢复。

Agent 契约参见 [Agent REST 数据库供应](agent-rest-provisioning.md)。迁移和混合版本
边界参见[升级与回滚](../deployment/upgrade-and-rollback.md)。
