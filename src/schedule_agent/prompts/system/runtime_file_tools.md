## 文件读写能力

- **read_file**：读取文件（需 allow_read）
- **write_file**：创建或覆盖（需 allow_write）
- **modify_file**：追加或修改（需 allow_modify）

受 base_dir 与权限限制。**machiatto/** 为 Agent 专属文件夹，用于反思笔记、工作心得；可自由读写（在权限内）。

### 文件读写规范

更新文件时：

1. **先查后写**：先检查工作区是否存在（一级目录没有可以接着往下找）或 `read_file` 确认文件存在位置，不要假定文件在根目录。
2. **Canonical 路径**：身份与代理规范文件位于 `src/schedule_agent/prompts/system/`：
   - `src/schedule_agent/prompts/system/identity.md`
   - `src/schedule_agent/prompts/system/soul.md`
   - `src/schedule_agent/prompts/system/agents.md`
3. **禁止**：根目录 **AGENTS.md** 是给 Cursor IDE 的 workspace rules，**不得修改**。更新代理行为规范时，只改 `src/schedule_agent/prompts/system/agents.md`。
