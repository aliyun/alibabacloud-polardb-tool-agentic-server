# 发布流程

[English](../../en/reference/release-process.md)

语义版本号与 GitHub Release 的成熟度是两个独立决定。只要受支持部署、升级、
回滚和已知问题门禁均已通过，即使项目仍处于 `v0.0.x` 系列，也可以发布为
稳定 Release。只有当某个准确版本明确用于评估、尚未被接受为稳定版本时，才
使用 Pre-release。

## 仓库保护

保护公开 `main`，要求 CI 和 Review，并禁止 force push 与重写 tag。为
`release` GitHub Environment 配置维护者审批。发布工作流只接受位于公开
`main` 历史上的 `vMAJOR.MINOR.PATCH` tag，而且 tag 必须与 Python、Web、
lockfile、Chart 和 app 版本一致。

某些仓库配置下 GHCR 新 Package 默认为私有。首次受控推送后，仓库 Owner
必须把镜像 Package 和 OCI Chart Package 都设为 Public，并确认它们与仓库
关联。工作流会主动退出登录并匿名读取镜像和 Chart；任一 Package 仍为私有
时，会在创建 Release 前停止。

## 公开快照提交

将已验证的 `develop` 快照作为新的增量 commit 应用到公开 `main`；不得 amend
或替换已有公开历史。提交主题必须使用允许的 Conventional Commit 类型，并
描述用户可见功能或修复，不能只写版本号或发布动作。例如：

```text
fix: harden dedicated pool networking and endpoint selection

Release-Version: vMAJOR.MINOR.PATCH
Source-Develop: 0123456789abcdef0123456789abcdef01234567
```

两个必需 trailer 分别记录语义发布版本和准确的内部源码 commit，主题不能
简化成 `publish v0.0.x` 或 `port develop`。

内部 `develop` 与公开 `main` 之间不存在可依赖的 commit 祖先关系。准备下一次
快照时，应从准确的目标 `develop` commit 导出 allowlist 允许的公开 tree，再与
当前公开 `main` tree 比较，只把 tree 的净差异作为新的公开 commit。不得根据
`Source-Develop..develop` 推导发布范围，也不得直接 cherry-pick 一段内部 commit：
较早的功能内容可能已经以不同 commit 身份进入公开 `main`。新 commit 的
`Source-Develop` trailer 必须填写本次已审核导出 tree 对应的准确内部 commit。

## Draft 检查

受保护工作流生成不可变的多架构镜像与 Chart、分架构离线镜像、SPDX SBOM、
校验和及 GitHub attestations，最后创建 **Draft** GitHub Release，并默认
启用 Pre-release 标记。这个初始标记是安全的审核默认值，不代表最终成熟度；
工作流不会自动发布 Release。

批准发布的维护者必须检查：

- CI、迁移、镜像、Helm、公开导出、密钥扫描和许可证门禁。
- 镜像 manifest digest 与 AMD64/ARM64 平台 digest。
- Chart 版本/digest，以及两个 GHCR Package 的匿名访问。
- 资产名称、校验和、attestation 和 SBOM 漏洞。
- 自动生成的 Release notes、已知问题、升级限制和中国网络离线说明。

## 发布状态决策

Draft 检查通过后，直接选择以下一种状态发布。不要先发布成 Pre-release，再把
它转为稳定版作为中间步骤。

发布稳定版本时，清除 Pre-release 标记并明确设为 GitHub Latest：

```bash
gh release edit "${RELEASE_TAG}" \
  --draft=false \
  --prerelease=false \
  --latest \
  --verify-tag
```

发布试用版本时，保留 Pre-release 标记，不要设置为 GitHub Latest：

```bash
gh release edit "${RELEASE_TAG}" \
  --draft=false \
  --prerelease \
  --verify-tag
```

发布后检查 Release 的 `draft` 和 `prerelease` 字段、稳定的资产下载 URL 与
校验和、tag commit，以及 `published` 事件触发的全部工作流。稳定版本还要
确认 `/releases/latest` 已选择预期 tag。

接受漏洞例外时，应记录范围、理由、Owner 和到期时间；不能隐藏或静默忽略
扫描发现。例外记录在公开的
[`dependency-vulnerability-exceptions.yaml`](../../../security/dependency-vulnerability-exceptions.yaml)
清单中。依赖安全策略测试会拒绝已过期的例外；到期前必须删除例外、重新评审
并续期，或升级到已修复的依赖版本。

## 容器镜像 `latest` 别名

当前发布 GitHub Release 后，如果候选版本是已发布语义版本中的最高版本，
`published` 事件工作流会将其已验证的容器镜像 digest 提升为可变的 `latest`
别名。该工作流也会处理 Pre-release，因此试用版本即使不是 GitHub Latest，
也可能更新容器别名。延迟发布的旧 Release 无法让别名回退。该别名只适用于
容器镜像，不会创建或替换 Chart 版本。

`latest` 仅用于试用和发现。生产部署及需要可复现的部署仍必须固定准确的语义
版本，最好直接固定已验证的镜像 digest。

## 不可变策略

不得替换已发布的 tag、镜像、Chart、归档、校验和或 Release 资产。发现缺陷
时发布新的 patch 版本。同一 tag 已存在 Release 时，工作流重跑会失败。
只调整 GitHub Release 成熟度元数据时，也不得改动任何不可变制品。

## 恢复未完成的 Release

如果不可变 tag、镜像和 Chart 已存在，但 GitHub Release 未创建，可使用手工
恢复工作流。它会校验准确的 tag commit、该提交是否位于公开 `main` 历史、
标签源码的全部版本、镜像标签与平台 digest、Chart 可读性，以及 Release
确实不存在。工作流不会重建或重新推送已有版本的镜像或 Chart。

将 `RELEASE_TAG` 和 `EXPECTED_COMMIT` 设置为未完成 Release 的准确值，再先
运行只读预检：

```bash
RELEASE_TAG="${RELEASE_TAG:?set the existing vMAJOR.MINOR.PATCH tag}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set the exact 40-character tag commit}"

gh workflow run recover-release.yml \
  -f tag="${RELEASE_TAG}" \
  -f expected_commit="${EXPECTED_COMMIT}" \
  -f dry_run=true
```

检查任务摘要中的 JSON 证据。只有确认无误后，维护者才能把 `dry_run` 改为
`false` 启动写入任务。该任务必须通过 `release` Environment 审批，并创建
默认启用 Pre-release 标记、供人工检查的 **Draft**。它不会自动发布 Draft；
恢复检查通过后，仍按上面的发布状态决策操作。
