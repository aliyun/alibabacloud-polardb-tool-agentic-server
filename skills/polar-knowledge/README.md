# Polar 知识库问答 Skill

[polar-knowledge](SKILL.md)：通过 PAS MCP 对一个知识资源进行中英文分路检索并引用证据回答，无需脚本或额外依赖。

## 前提

- 按照[连接 MCP 客户端](https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/blob/main/docs/zh-cn/agents/connect-mcp-client.md)和[知识库接入与企业身份](https://github.com/aliyun/alibabacloud-polardb-tool-agentic-server/blob/main/docs/zh-cn/knowledge/polarrag-mcp.md)配置有权访问目标知识库的用户连接，提供 `list_knowledge_resources` 和 `kb_search`。
- 目标 Space 已配置 reranker，否则返回 `RERANKER_NOT_CONFIGURED`。
- 凭据仅在客户端配置，不放入 Skill 目录；安装 Skill 不授予访问权限。

## 安装与使用

macOS Qwen Work 国内版，在仓库根目录执行：

```bash
"/Applications/QwenWorkCN.app/Contents/Resources/bin/qoderclicn" \
  --config-dir "$HOME/.qwenworkcn" \
  skills install "$PWD/skills/polar-knowledge" --scope user --consent
```

其他兼容客户端使用各自的安装工具安装同一目录。

例如：“使用 polar-knowledge，查询 Team Research 知识库中项目 A 的交付日期，并给出来源。”
