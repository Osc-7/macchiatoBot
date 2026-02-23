# 技能 (Skills)

技能已迁移至 **Skills CLI 默认目录** `~/.agents/skills`，与 `npx skills add -g` 统一管理。

## 添加技能

**方式一：npx skills（推荐）**

```bash
npx skills find <keyword>        # 搜索技能
npx skills add <owner/repo@skill> -g -y   # 安装到 ~/.agents/skills
```

**方式二：手动**

1. 在 `~/.agents/skills/` 下新建 `{skill-name}/SKILL.md`
2. 符合 [AgentSkills](https://agentskills.io/) 规范（YAML frontmatter + Markdown 正文）
3. 若需仅展示部分技能，在 `config.yaml` 中设置 `skills.enabled: [skill-name]`；为空则展示全部

## 渐进式披露

System prompt 仅注入 metadata；需完整内容时调用 `load_skill(skill_name)` 按需加载。
