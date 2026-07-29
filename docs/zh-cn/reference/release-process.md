# 发布流程

[English](../../en/reference/release-process.md)

`v0.0.x` 是供用户试用的预发布版本。只有经过实际反馈和缺陷修复，受支持部署
达到稳定状态后，才升级为 `v0.1.0`。

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
fix: harden resource pool networking and endpoint selection

Release-Version: vMAJOR.MINOR.PATCH
Source-Develop: 0123456789abcdef0123456789abcdef01234567
```

两个必需 trailer 分别记录语义发布版本和准确的内部源码 commit，主题不能
简化成 `publish v0.0.x` 或 `port develop`。

## Draft 检查

受保护工作流生成不可变的多架构镜像与 Chart、分架构离线镜像、SPDX SBOM、
校验和及 GitHub attestations，最后创建 **Draft、Pre-release** GitHub
Release；不会自动发布。

批准发布的维护者必须检查：

- CI、迁移、镜像、Helm、公开导出、密钥扫描和许可证门禁。
- 镜像 manifest digest 与 AMD64/ARM64 平台 digest。
- Chart 版本/digest，以及两个 GHCR Package 的匿名访问。
- 资产名称、校验和、attestation 和 SBOM 漏洞。
- 自动生成的 Release notes、已知问题、升级限制和中国网络离线说明。

接受漏洞例外时，应记录范围、理由、Owner 和到期时间；不能隐藏或静默忽略
扫描发现。

## 容器镜像 `latest` 别名

发布 GitHub Release 后，可将其已验证的容器镜像 digest 提升为可变的
`latest` 别名。只有候选版本是已发布语义版本中的最高版本时才会提升，因此
延迟发布的旧 Release 无法让别名回退。该别名只适用于容器镜像，不会创建或
替换 Chart 版本。

`latest` 仅用于试用和发现。生产部署及需要可复现的部署仍必须固定准确的语义
版本，最好直接固定已验证的镜像 digest。

## 不可变策略

不得替换已发布的 tag、镜像、Chart、归档、校验和或 Release 资产。发现缺陷
时发布新的 patch 版本。同一 tag 已存在 Release 时，工作流重跑会失败。
整个 `v0.0.x` 系列保持 `prerelease` 标记。

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
供人工检查的 **Draft、Pre-release**，不会自动发布 Draft。
