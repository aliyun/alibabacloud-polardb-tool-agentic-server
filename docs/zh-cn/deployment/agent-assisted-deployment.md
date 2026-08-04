# Agent 辅助的单机部署

[English](../../en/deployment/agent-assisted-deployment.md) | **简体中文**

仓库提供一个部署 SKILL，用于用户明确要求的 PAS 单机部署。该 SKILL 在 Docker
Compose 与源码模式中选择其一，先验证 Linux 目标机再执行变更，默认部署 PAS
`0.0.5`。

## 范围与 Agent 发现

规范 SKILL 位于 `.agents/skills/deploy-polardb-agentic-server/SKILL.md`。
Codex 和 Cursor 可以发现该 Agent Skills 路径。Claude Code 使用
`.claude/skills/` 下的同步副本，其元数据还会禁止模型自行触发。

请显式调用 `deploy-polardb-agentic-server`，并选择一种模式：

- Docker Compose：适合生产化单机部署，控制台与 API 共用一个端口。
- 源码：适合开发、代码定制、仅后端运行或目标机没有 Docker 的场景。

脚本只面向 Linux。Mac 可以通过 SSH 作为控制端，但不是 PAS 原生部署目标。

## 安全与版本固定

默认 Release 设置如下：

```bash
PAS_VERSION=0.0.5
PAS_REF=v${PAS_VERSION}
PAS_IMAGE=ghcr.io/aliyun/alibabacloud-polardb-tool-agentic-server:${PAS_VERSION}
```

新建或更新 checkout 时会获取 `PAS_REF` 并使用 detached HEAD。当
`PAS_UPDATE_REPO=1` 时，已有 checkout 必须无改动，且 `origin` 必须与
`PAS_REPO` 一致。脚本不会跟随仓库默认分支。

不要在 Agent 对话或命令行中放入数据库密码、连接 URL、加密密钥或 bootstrap
token。应在目标机把数据库密码写入仅 owner 可读的文件（`0600`），再通过
`POLARDB_PASSWORD_FILE` 传入其路径。脚本会把 bootstrap token 保存到
`$PAS_HOME/.secrets/bootstrap_token.txt`，只能在目标机读取。

## 验证 Linux 目标机

在包含该 SKILL 的 checkout 中，Mac 或 Linux 控制端可以通过 SSH 标准输入传输
验证脚本。该方式不使用 `scp`，也不会复制密钥：

```bash
SKILL_DIR=.agents/skills/deploy-polardb-agentic-server
ssh user@linux-host \
  "POLARDB_HOST='db-endpoint' POLARDB_USER='pas_user' \
   PAS_HOME='/data/polar-mcp' bash -s -- --validate-only" \
  < "$SKILL_DIR/scripts/deploy-docker.sh"
```

只有选择源码模式后才改用 `deploy-source.sh`。验证会检查 Linux、输入、数据库
TCP 连通性、目标目录安全性、已有仓库身份和对应模式的运行路径。它不会读取
密码，也不会修改软件包、文件、镜像、进程或服务。

## 运行所选模式

验证通过且运维人员批准变更后，在 Linux 目标机直接创建密码文件，再使用同一
脚本并移除 `--validate-only`。例如，Docker 模式可以直接流式执行而无需复制
脚本：

```bash
ssh user@linux-host \
  "POLARDB_HOST='db-endpoint' POLARDB_USER='pas_user' \
   POLARDB_PASSWORD_FILE='/secure/polardb-password' \
   PAS_HOME='/data/polar-mcp' bash -s" \
  < "$SKILL_DIR/scripts/deploy-docker.sh"
```

Docker 模式默认使用已发布的 `0.0.5` 镜像，镜像无法拉取时会失败。需要时应把
`PAS_IMAGE` 设置为经过批准的完整镜像地址。源码模式使用固定的 `v0.0.5`
checkout 和冻结的 Python lock 构建；仅部署后端时设置 `SKIP_WEB=1`。

## 显式专家覆盖项

`PAS_UPDATE_REPO=0` 会保留运维人员有意预置的 PAS checkout，跳过 fetch 与
checkout。脚本仍会验证项目标识，但此时 commit 来源和依赖兼容性由运维人员
负责。

只有显式设置 `PAS_ALLOW_LOCAL_BUILD=1` 时，Docker 模式才会本地构建。本地
构建使用同一个已验证 checkout，但不等同于已发布镜像的来源证明。不要仅为
绕过镜像仓库或架构错误而启用该回退。

## 验证、回滚与移除

两种模式部署后都应验证 `http://127.0.0.1:18760/readyz`。Docker 运维人员还应
检查 Compose 项目；源码模式应检查 `run/backend.out`，启用 Web 时还应检查
`run/web.out`。入站访问只应放行必要来源。

部署脚本不会删除数据，也不会回滚 Schema 迁移。切换版本前，应备份元数据库、
根密钥和部署配置，并遵循[升级与回滚指南](upgrade-and-rollback.md)。

如需停止 Docker 模式但保留 `$PAS_HOME`，执行：

```bash
cd /data/polar-mcp
docker compose -p polardb-agentic \
  --env-file .secrets/pas-compose.env \
  -f deploy/compose/compose.external-mysql.yaml down
```

源码模式应先核对每个 PID 对应的 `/proc/<pid>/cwd` 和命令行，再停止进程；禁止
使用宽泛的 `pkill`。只有在运维人员已单独保存或明确放弃其中的密钥、日志和
数据后，才能移除 `$PAS_HOME`。数据库清理属于另一项需要明确执行的操作。
