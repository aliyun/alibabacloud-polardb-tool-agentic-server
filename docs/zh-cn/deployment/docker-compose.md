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

已有 MySQL 8.0 数据库：

```bash
export PAS_DATABASE_URL='mysql+asyncmy://USER:PASSWORD@HOST:3306/DATABASE'
docker compose \
  -f deploy/compose/compose.external-mysql.yaml \
  up -d
```

PostgreSQL：

```bash
export PAS_DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE'
docker compose \
  -f deploy/compose/compose.external-postgres.yaml \
  up -d
```

应通过权限受限的环境文件或密钥管理系统提供 `PAS_DATABASE_URL` 和
`PAS_ENCRYPTION_KEY`，不要把它们留在 shell 历史中。

## 停止或删除

`docker compose down` 会保留命名卷。只有在完成并验证备份、确认可以永久删除
元数据、日志和 bootstrap 交换卷时，才使用
`docker compose down --volumes`。
