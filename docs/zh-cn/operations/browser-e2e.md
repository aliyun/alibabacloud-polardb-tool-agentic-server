# 浏览器端到端测试

[English](../../en/operations/browser-e2e.md)

Playwright 测试会构建控制台，并为每个 case 启动真实 PAS 进程、执行真实
SQLite 迁移。浏览器操作调用真实 HTTP API，不使用模拟响应，也不接受已有
服务 URL 或数据库。

## 本地运行

从仓库根目录执行：

```bash
uv sync --locked --extra dev
cd web
npm ci
npx playwright install chromium
npm run test:e2e
```

Linux 使用 `npx playwright install --with-deps chromium`。默认使用仓库的
`.venv/bin/python`，不存在时使用 `python3`；可通过 `E2E_PYTHON` 指定解释器。
在 `web/` 下还可以执行：

```bash
npm run typecheck:e2e
npx playwright test --grep 'department'
npx playwright test --headed
npm run test:e2e:report
```

直接执行 `playwright test` 前需要已有 `npm run build` 构建产物。
升级 Playwright 后需再次执行 `npx playwright install chromium`，安装匹配版本的
浏览器。找不到浏览器可执行文件表示缺少测试依赖，应安装后重新运行。

如需测试已安装的 Google Chrome，显式选择对应 channel：

```bash
E2E_BROWSER_CHANNEL=chrome npm run test:e2e
```

默认仍使用 Playwright 固定版本的 Chromium。使用 `chrome` channel 前必须安装
Chrome，runner 不会自动替换浏览器。

## 覆盖范围

| Case | 验证行为 |
| --- | --- |
| 全新安装 | 认领、dry-run、激活管理员、登录、仪表盘、刷新及服务重启 |
| 部门 | 通过 UI 创建、重命名、刷新和删除 |
| 用户 | 创建普通成员，独立登录并拒绝管理访问 |
| 账户布局 | 持久化长目录身份，在英文和简体中文的 1440/1024 px 视口中容纳表格，并打开/关闭企业身份详情 |
| 钉钉 callback | 未配置公共 URL 时显示可操作的配置错误，保持验证可重试，不创建授权状态 |
| Agent | 通过 UI 创建、禁用，刷新后状态保持 |
| 知识功能 | 保存连接草稿，未验证访问权限时禁止激活 |
| 公网边界 | 拒绝匿名业务/发现请求，禁用在线 API 文档 |
| OpenAPI 端点 | 显式选择网络/地域，预览官方公网/VPC 与 STS 端点，拒绝目录缺失组合，保护需认证的端点目录 |
| OpenAPI 配置 | 显示安全的预检失败诊断，编辑后失效旧计划，显式确认激活，复用已存凭据，重启后保持配置 |
| 资源地域 | 新资源池采用生效默认值，切换地域清空网络位置字段，限制 VPC 地域，保留已有资源池且 SQL-only 实例地域可选 |

管理员用例遇到未捕获的浏览器 JavaScript 异常会失败。仪表盘用例保存截图；
这是功能 UI 测试，不是像素比对或无障碍认证。真实上游知识激活/检索、云资源
创建、provider 编排、外部 SSO、MySQL/PostgreSQL 和多副本部署需要独立集成测试。

OpenAPI case 使用无效的一次性凭据和真实 SDK。`web/e2e/pas_runner.py` 中的
Python audit guard 在网络 I/O 前拒绝非回环 DNS/TCP/UDP，不伪造 PAS 或上游
API 响应。用例验证连接失败及显式确认继续的路径，不验证云端认证成功或 VPC
连通性。资源池预热目标设为零，不启用 worker。真实 OpenAPI 成功检查仍需在
部署 PAS 的网络内单独验收。

后端 SDK 契约测试另用回环 HTTP mock 验证 OpenAPI 成功与拒绝响应。本地数据库、
Compose 及其他需显式启用的验收见开发
[本地回归指南](../../../scripts/dev/local-regression.zh-CN.md)。

## 隔离与诊断

每个 case 使用随机回环端口、临时 SQLite 数据库、加密密钥和管理员密码，
清除继承的 `PAS_*`、`ALIBABA_CLOUD_*` 环境变量、代理设置和 `PYTHONPATH`。
runner 仅允许回环网络访问；这是测试保护机制，不是操作系统安全沙箱。
fixture 在退出时仅终止自己
启动的服务，仅删除自己创建的临时数据目录。不修改 provider 接口或生产配置。
强制杀死测试进程可能导致清理无法完成。
布局数据直接写入该私有 SQLite 数据库；seed helper 拒绝其他数据库及 fixture
临时目录之外的路径。浏览器仍通过真实 API 读取持久化数据。

失败截图及 Playwright trace 保存在 `web/test-results/e2e`，HTML 报告位于
`web/playwright-report`。服务日志会脱敏 bootstrap token。trace 可能包含一次性
测试凭证，应限制产物访问权限，case 中不得使用真实凭证。这些目录已被 Git 忽略。

**Browser E2E** workflow 在面向 `main`/`develop`/`release/**` 的 push 和 pull request 时执行
Chromium 测试，也支持手动触发。产物保留七天，不自动重试，避免掩盖失败。
Vitest 继续仅运行 `src/**/*.test.{ts,tsx}`。

在 `web/e2e/` 下添加用例：`pas` 提供独立服务，`adminPage` 提供通过浏览器完成
初始化的管理员页面。各 case 应独立，不复用其他 case 数据，不通过浏览器 mock
替代真实 API 响应。
