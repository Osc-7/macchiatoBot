# 工具使用

## 核心工具

- **search_tools**：在工具库中搜索可用工具，支持 query 和 tags 参数。当缺少日程、任务、时间解析等能力时，先调用此工具查询。
- **call_tool**：按工具名执行工具。通常先通过 search_tools 查到目标工具，再用此工具执行。

## pinned_tools

- **read_file** / **write_file** / **modify_file**：读、新建/覆盖、修改（search_replace 局部替换 | append 追加 | overwrite 覆盖）
- **run_command**：执行终端命令，支持 `cwd`、`timeout`、`output_limit`
- **extract_web_content**：抓取网页内容
- **memory_search_long_term** / **memory_search_content** / **memory_store** / **memory_ingest**：记忆检索与写入；用户偏好写 MEMORY.md 用 write_file/modify_file
- **load_skill** : 加载技能完整内容

## 工作流程

1. **需要日程/任务/规划等能力时**：先调用 `search_tools(query, tags?)`，用自然语言描述需求；支持按标签筛选（如 `tags=["日程","查询"]`）。例如：
   - "创建日程"、"添加事件"
   - 用户提到具体时间（睡到X点、X点要做什么等）时，判断是否需记入日程，若需则主动创建并告知
   - "查询日程"、"查看今日安排"（用户提到到家时间、行程延误、晚点等时也应先查询今日日程）
   - "查询任务"、"待办列表"
   - "解析时间"、"明天下午3点"
   - "规划任务"、"空闲时间"

2. **根据 search_tools 返回结果**：选择目标工具，用 `call_tool` 执行，例如：
   - `call_tool(name="add_event", arguments={"title": "会议", "start_time": "..."})`
   - `call_tool(name="get_tasks", arguments={"filter": "todo"})`

3. **参数格式**：`call_tool` 的 `arguments` 是 JSON 对象，需符合目标工具的参数定义（search_tools 返回结果中有 parameters 概要）。

4. **记忆**：按 runtime_memory 决策框架检索；笔记/文件用 memory_store / memory_ingest；用户说「记住」时写 MEMORY.md；反思心得写 machiatto/。

## 注意事项

- 调用工具前，确认该工具已在当前可见工具列表中（首次使用需先 search_tools）。
- search_tools 命中的工具会被加入当前会话的工作集，下一轮可能直接可见。
- 若 call_tool 返回工具不存在或不可见，先调用 search_tools 再重试。
