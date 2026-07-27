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

## 不可变策略

不得替换已发布的 tag、镜像、Chart、归档、校验和或 Release 资产。发现缺陷
时发布新的 patch 版本。同一 tag 已存在 Release 时，工作流重跑会失败。
整个 `v0.0.x` 系列保持 `prerelease` 标记。
