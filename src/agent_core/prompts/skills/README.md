# 技能 (Skills)

技能目录（按优先级）：

1. **`.macchiato/skills`** — 本工作区 / 本机专用
2. **`.agents/skills`** — Skills CLI 默认目录（`npx skills add -g`）

同名技能以 `.macchiato` 为准。远程模式下索引与 `load_skill` 都读**当前远程工作区**这两处。

## 添加技能

**方式一：npx skills（推荐）**

```bash
npx skills find <keyword>        # 搜索技能
npx skills add <owner/repo@skill> -g -y   # 安装到 ~/.agents/skills
```

在隔离或远程工作区里，bash 的 `~` 即工作区根，因此会装到该工作区的 `.agents/skills`。

**方式二：手动**

1. 在 `.macchiato/skills/` 或 `.agents/skills/` 下新建 `{skill-name}/SKILL.md`
2. 符合 [AgentSkills](https://agentskills.io/) 规范（YAML frontmatter + Markdown 正文）
3. 若需仅展示部分技能，在 `config/config.yaml` 中设置 `skills.enabled: [skill-name]`；为空则展示全部

## 渐进式披露

System prompt 仅注入 metadata；需完整内容时调用 `load_skill(skill_name)` 按需加载。
远程工作区在 `/remote-use`（或任务绑定远程）时会扫描远程技能树并缓存索引，供后续 system prompt 注入。
工作区切换写入对话历史（`[工作区切换]`），不再挂在 system prompt；上下文压缩后若仍在远程，会再注入一条 `[工作区]` 状态说明（含具体路径，不提压缩）。
