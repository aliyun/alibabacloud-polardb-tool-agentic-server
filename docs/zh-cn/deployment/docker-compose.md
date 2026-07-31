# Docker Compose 部署

[English](../../en/deployment/docker-compose.md)

受支持的 Compose 栈依次运行三个服务：固定版本的 MySQL 8.0、一次性元数据库
迁移和 PAS 服务。默认只向宿主机开放 PAS `18760` 端口，MySQL 只存在于
Compose 私有网络。

## 准备密钥

复制示例文件，不要提交生成的 `.env`：

```bash
cp .env.compose.example .env
chmod 0600 .env
```

将根密钥直接写入权限受限的文件，避免输出到 CI 日志：

```bash
python3 -c \
  'import base64,os; print(base64.b64encode(os.urandom(32)).decode())' \
  | sed 's/^/PAS_ENCRYPTION_KEY=/' >> .env
```

删除原来的 `PAS_ENCRYPTION_KEY` 占位行，设置强且互不相同的 MySQL 密码，并
单独备份 `.env`。重启和升级后必须继续使用同一根密钥。

`MYSQL_IMAGE` 默认指向经过测试的固定 digest。在受限网络或中国大陆网络中，
先把同一镜像同步到可访问的镜像仓库，再将 `MYSQL_IMAGE` 设置为镜像仓库中的
引用后启动。

## 启动与检查

```bash
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:18760/readyz
```

只有 `migrate` 成功退出后，`server` 才会启动。升级失败时检查：

```bash
docker compose logs migrate
docker compose logs server
```

按照初始化指南中的 bootstrap token 流程接管服务。应用日志和 Pod 本地 token
交换目录使用命名卷，MySQL 数据使用 `mysql-data`。

## 备份与升级

升级前同时备份 MySQL 数据库和根加密密钥。把新版本解析为 digest，将
`PAS_IMAGE` 设置为该精确镜像，然后执行：

```bash
docker compose pull
docker compose run --rm migrate database migrate
docker compose up -d --no-deps server
```

如果迁移没有到达应用要求的 Alembic head，服务会拒绝启动。

## 外部元数据库

使用已有 MySQL 8.0 数据库时，通过工具生成权限受限的环境文件，不要在
shell 历史中手工拼接 URL：

```bash
scripts/deploy/create-external-mysql-env.sh --output .env.external-mysql
```

工具会收集 endpoint、端口、数据库名与用户名，然后展示这些字段供确认。
在 `Use these settings? [Y/n]` 输入 `n` 可以重新填写；输入密码时显示为
`*`。如果填写了 Docker loopback 地址，接受
`Use host.docker.internal instead? [Y/n]` 后会在最终确认前真正完成替换。

写入文件前，工具会打印非敏感连接目标并执行 `SELECT 1`。认证失败、数据库
不存在、DNS 解析失败与连接不可达都会返回不含密码和完整 URL 的具体原因。
测试失败不会生成文件，修正输入后可重复执行同一命令。成功生成后若需修改，
请使用新的输出文件名；工具不会覆盖已有文件。

使用生成的文件先迁移，再启动服务：

```bash
docker compose --env-file .env.external-mysql \
  -f deploy/compose/compose.external-mysql.yaml run --rm migrate
docker compose --env-file .env.external-mysql \
  -f deploy/compose/compose.external-mysql.yaml up -d server
```

镜像已同步到其他仓库或正在验证候选镜像时，使用 `--image IMAGE`。选定的镜像
会写入生成文件，确保后续 Compose 命令使用同一镜像。完整流程见
[单台 ECS Compose 部署教程](../getting-started/deploy-compose.md)。

连接测试默认失败即停止。只有明确接受未验证配置时才使用
`--skip-connection-test`；连接信息错误会导致后续迁移或启动失败。

非交互自动化可以直接生成环境文件，但必须分别对 URI 组件进行百分号编码，
并使用单引号保护 `$` 等字符。不要把原始密码拼入 URL，也不要复制仅供自带
MySQL 使用的 `.env.compose.example`。运行
`scripts/deploy/create-external-mysql-env.sh --help` 查看完整参数。

```dotenv
PAS_DATABASE_URL='mysql+asyncmy://USER:PERCENT_ENCODED_PASSWORD@ENDPOINT:3306/DATABASE'
PAS_ENCRYPTION_KEY='BASE64_ENCODED_32_BYTE_KEY'
```

PostgreSQL：

```bash
export PAS_DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE'
docker compose \
  -f deploy/compose/compose.external-postgres.yaml \
  up -d
```

PostgreSQL 场景应通过权限受限的环境文件或密钥管理系统提供
`PAS_DATABASE_URL` 和 `PAS_ENCRYPTION_KEY`，不要把它们留在 shell 历史中。

## 停止或删除

`docker compose down` 会保留命名卷。只有在完成并验证备份、确认可以永久删除
元数据、日志和 bootstrap 交换卷时，才使用
`docker compose down --volumes`。
