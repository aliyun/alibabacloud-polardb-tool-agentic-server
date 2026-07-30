# 部署（单台 ECS + Docker Compose）

[English](../../en/getting-started/deploy-compose.md) | **简体中文**

本页在一台 ECS 上使用 Docker Compose 部署 PAS，元数据库指向
[资源要求](./cloud-resources.md)中准备好的 PolarDB MySQL。

## 前置条件

- 已获得 ECS 的 SSH 登录方式，并拥有 sudo 权限。
- 已获得 PolarDB 的连接地址、数据库名、用户名与密码。
- ECS 安全组已放通 TCP 18760。

## 第一步：安装 Docker

登录 ECS 后安装 Docker Engine 与 Compose 插件：

```bash
curl -fsSL https://get.docker.com | sh
sudo docker version
sudo docker compose version
```

注意：Compose 是独立插件，部分安装方式（如通过 yum 直接安装 docker）不会
自动带上。若 `docker compose version` 报错「不是 docker 命令」，需单独
安装插件：

```bash
sudo yum -y install docker-compose-plugin
sudo docker compose version
```

详细安装方案参考
[在 ECS 上安装并使用 Docker](https://help.aliyun.com/zh/ecs/user-guide/install-and-use-docker)。

## 第二步：下载部署文件

将 `PAS_VERSION` 设置为要部署的版本，再从 GitHub 下载对应标签的源码包。
源码包包含 `deploy/compose/` 下的 Compose 部署文件，无需安装 Git：

```bash
PAS_VERSION=0.0.5
wget "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/archive/refs/tags/v${PAS_VERSION}.tar.gz"
tar -xzf "v${PAS_VERSION}.tar.gz"
cd "alibabacloud-polardb-tool-agentic-server-${PAS_VERSION}"
```

后续命令均在该目录内执行。服务镜像无需手动下载，启动时会自动拉取该版本
部署文件指定的镜像；也可提前执行 `docker pull` 预热。习惯使用 Git 时，
也可以 `git clone` 仓库后切换到对应的 `v${PAS_VERSION}` 标签，目录结构
相同。

## 第三步：准备 .env

使用当前发布版本提供的容器化工具生成 `PAS_DATABASE_URL` 与
`PAS_ENCRYPTION_KEY`：

```bash
scripts/deploy/create-external-mysql-env.sh
```

宿主机只运行 POSIX shell 与 Docker；Python、SQLAlchemy 和 `asyncmy`
驱动均在选定的 PAS 镜像内运行。工具会依次提示连接地址、端口（默认
`3306`）、数据库名和用户名，然后展示这些非敏感字段并询问
`Use these settings? [Y/n]`；输入 `n` 可重新填写。密码输入会显示为 `*`
但不会暴露实际内容。工具会安全编码特殊字符，构造 `mysql+asyncmy` URL，
并在生成加密根密钥或创建 `.env` 前打印连接地址、数据库、用户名以及将要
执行的 `SELECT 1` 动作。失败时会给出脱敏后的具体原因，例如认证失败、
数据库不存在、域名解析失败或连接地址不可达。

通过 Docker 运行工具时，`127.0.0.1` 与 `localhost` 指向生成器容器自身，
不是宿主机。若 MySQL 运行在使用 Docker Desktop 的 macOS 或 Windows
宿主机上，工具会询问 `Use host.docker.internal instead? [Y/n]`，确认后
会真正替换 endpoint；其他环境应填写容器能够访问的 DNS 名称或 IP 地址。

成功后 `.env` 权限为 `0600`。工具拒绝覆盖已有路径；连接或写入失败时不会
留下新的环境文件。不要通过命令行参数传递数据库字段或密码。

如果发布镜像已经同步到其他镜像仓库，请显式选择：

```bash
scripts/deploy/create-external-mysql-env.sh \
  --image registry.example/pas:VERSION
```

显式指定的 `--image` 也会作为 `PAS_IMAGE` 写入文件，确保后续 Compose
命令使用同一个镜像。工具会忽略宿主环境中继承的 `PAS_IMAGE`、
`PAS_DATABASE_URL` 与 `PAS_ENCRYPTION_KEY`。中国大陆网络请在执行该命令
前先将镜像同步到可访问的仓库。

连接测试默认采用失败即停止策略。只有数据库尚不可达且你明确接受未验证
配置时，才使用：

```bash
scripts/deploy/create-external-mysql-env.sh --skip-connection-test
```

该选项会打印警告；如果连接信息有误，后续迁移或启动仍会失败。

对于无法使用交互终端的高级自动化场景，只有在分别完成各 URI 组件的百分号
编码后才手工创建文件。保留单引号，使 Compose 将 `$` 等字符视为字面量：

```dotenv
PAS_DATABASE_URL='mysql+asyncmy://USER:PERCENT_ENCODED_PASSWORD@ENDPOINT:3306/DATABASE'
PAS_ENCRYPTION_KEY='BASE64_ENCODED_32_BYTE_KEY'
```

切勿将包含 URI 分隔符的原始密码直接拼入 URL。不要复制
`.env.compose.example`，它包含 `MYSQL_ROOT_PASSWORD` 等变量，仅供自带
MySQL 的根目录 `compose.yaml` 使用。可按需追加 `PAS_PORT`。请妥善备份
`.env`，重启与升级必须使用同一个根密钥。

## 第四步：执行迁移并启动

v0.0.3 及后续镜像已经正确安装 `pas` 入口与 server package，不需要覆盖
`PYTHONPATH`。如果从曾添加 `PYTHONPATH: /app` workaround 的 v0.0.1
部署升级，请从 `deploy/compose/compose.external-mysql.yaml` 中删除该行。

使用面向外部元数据库的 Compose 文件，先迁移再启动：

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml run --rm migrate
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml up -d server
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml ps
curl --fail http://127.0.0.1:18760/readyz
```

<p align="center">
  <img src="images/deploy-migrate-start.png" alt="迁移、启动与 readyz 检查输出" width="820">
</p>

`--env-file .env` 不可省略：`-f` 指向子目录中的 Compose 文件时，Compose
不会自动加载当前目录的 `.env`。`server` 只有在 `migrate` 成功退出后才会
启动。若迁移未到达期望的 Alembic head，服务会拒绝启动。

## 第五步：认领 Owner

首次启动时，bootstrap token 会打印到容器日志：

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml logs server
```

<p align="center">
  <img src="images/bootstrap-token-logs.png" alt="容器日志中的 bootstrap token" width="820">
</p>

若日志不可用或 token 已过期，可在容器内重新签发并查看。签发命令拒绝写入
已存在的文件，重复签发前需要先删除旧文件：

```bash
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  rm -f /var/run/pas/bootstrap-token
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  pas config bootstrap-token issue --output /var/run/pas/bootstrap-token
sudo docker compose --env-file .env -f deploy/compose/compose.external-mysql.yaml exec server \
  cat /var/run/pas/bootstrap-token
```

重新签发会使旧 token 立即失效；新 token 有效期 15 分钟。完成 setup 后请
删除该 token 文件。

在浏览器打开 `http://<ECS 公网地址>:18760/setup`，输入 token 并创建首个
管理员（密码至少 12 位）。注意：控制台页面只对浏览器请求（
`Accept: text/html`）返回，用 `curl` 直接访问根路径会得到 `Not Found`，
这不代表服务异常；Web 控制台与 API 都在 18760 端口上提供。完整的 token
生命周期与恢复方式见[初始化设置](../setup/initial-setup.md)。

<p align="center">
  <img src="images/setup-enter-token.png" alt="setup 页面输入 bootstrap token" width="820">
</p>

设置管理员密码：

<p align="center">
  <img src="images/setup-admin-password.png" alt="创建首个管理员" width="820">
</p>

## Kubernetes / ACK 多副本（说明）

本教程当前主打单台 ECS + Docker Compose。若需多副本生产部署，Helm Chart
已发布为 OCI 制品
`oci://ghcr.io/aliyun/charts/polardb-agentic-server`，详见
[Kubernetes 部署指南](../deployment/kubernetes-helm.md)与
[ACK 与 PolarDB](../deployment/ack-polardb.md)。

下一步：[功能使用①：引导式配置](./configure.md)。
