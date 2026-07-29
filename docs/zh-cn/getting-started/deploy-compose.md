# 部署（单台 ECS + Docker Compose）

[English](../../en/getting-started/deploy-compose.md) | **简体中文**

本页在一台 ECS 上使用 Docker Compose 部署 PAS，元数据库指向
[资源要求](./cloud-resources.md)中准备好的 PolarDB MySQL。

## 前置条件

- 已获得 ECS 的 SSH 登录方式，并拥有 sudo 权限。
- 已获得 PolarDB 的 `PAS_DATABASE_URL` 连接串。
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
PAS_VERSION=0.0.4
wget "https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/archive/refs/tags/v${PAS_VERSION}.tar.gz"
tar -xzf "v${PAS_VERSION}.tar.gz"
cd "alibabacloud-polardb-tool-agentic-server-${PAS_VERSION}"
```

后续命令均在该目录内执行。服务镜像无需手动下载，启动时会自动拉取该版本
部署文件指定的镜像；也可提前执行 `docker pull` 预热。习惯使用 Git 时，
也可以 `git clone` 仓库后切换到对应的 `v${PAS_VERSION}` 标签，目录结构
相同。

## 第三步：准备 .env

外部元数据库场景需要直接提供 `PAS_DATABASE_URL` 与 `PAS_ENCRYPTION_KEY`
两个变量。复制外部数据库配置模板，设置仅当前用户可读，并生成根密钥：

```bash
cp .env.example .env
chmod 0600 .env
python3 - <<'PY'
import base64
import os
from pathlib import Path

path = Path(".env")
text = path.read_text()
key = base64.b64encode(os.urandom(32)).decode()
path.write_text(text.replace("PAS_ENCRYPTION_KEY=\n", f"PAS_ENCRYPTION_KEY={key}\n"))
PY
```

编辑 `.env`：

- 将 `PAS_DATABASE_URL` 替换为[资源要求](./cloud-resources.md)中产出的
  PolarDB 连接串。
- 按需追加 `PAS_IMAGE`、`PAS_PORT`；中国大陆网络请先将镜像同步到可访问的
  镜像仓库，再引用同步后的地址。
- `.env.example` 是本教程使用的外部元数据库模板；不要复制
  `.env.compose.example`，后者包含 `MYSQL_ROOT_PASSWORD` 等变量，仅供自带
  MySQL 的根目录 `compose.yaml` 使用。
- 妥善备份 `.env`，重启与升级必须使用同一个根密钥。

<p align="center">
  <img src="images/env-file-example.png" alt=".env 填写示例" width="820">
</p>

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
