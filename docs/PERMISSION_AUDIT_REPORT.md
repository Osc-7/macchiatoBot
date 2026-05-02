# macchiatoBot 权限与安全审计报告

**审计范围**：仓库当前 `master` 上 Agent 工具链、工作区隔离、bash、IPC 与持久化 ACL 相关实现（以 `src/agent_core/`、`src/system/` 为主）。  
**审计性质**：静态代码审查与架构梳理；非渗透测试、非运行时取证。  
**文档版本**：2026-05-01  

---

## 1. 执行摘要

### 1.1 设计亮点

- **双层工具控制**：`CoreProfile`（白名单/黑名单/模式）在 `AgentKernel` 执行 `ToolCallAction` 时再次校验；与 InternalLoader 的「用户态过滤」形成纵深。
- **工作区隔离 + 路径解析统一**：`workspace_isolation_enabled` 下，`resolve_path_string_for_tool` / `expand_user_path_str_for_session` 将 `~` 与会话 `(source, user_id)` 对齐，减少「bash 写到 A、工具读到 B」类分裂。
- **`read_file` 强约束**：隔离模式下普通租户仅能读工作区根、临时目录、合并后的可写根（含 canonical memory）、`readable_roots.json` 与进程内 ephemeral 前缀；与 `request_permission(kind=file_read)` 闭环。
- **Bash 分层安全**：`BashSecurity` 结合 sub 模式白名单、危险模式正则、`WORKSPACE_WRITE_DENIED` 与 `request_permission(bash_dangerous_command)` 一次性 grant（`permission_id` + 命令逐字匹配）。
- **可选 OS 级隔离**：Linux 上 `runuser` + 最小子进程环境，降低 daemon 环境变量向租户 shell 泄露的风险（见 `bash_os_user.py`、`minimal_subprocess_env_for_runuser`）。

### 1.2 关键发现（按严重度）

| 严重度 | 编号 | 摘要 |
|--------|------|------|
| **高** | F-1 | `memory_ingest` → `ContentMemory.ingest_file` 对任意存在的 `source_path` 读文件并写入内容记忆，**未**复用 `read_file` 的隔离根检查；隔离模式下可构成「任意可读 → 内容记忆侧信道」或数据外泄。 |
| **高** | F-2 | `attach_image_to_reply` / `attach_file_to_reply` 使用 `expand_user_path_str_for_session` + `Path.resolve()` 校验存在性，**未**校验路径落在允许的读根内；可将工作区外的本地文件登记为飞书等平台待发附件（用户侧可见，属有意能力时需明确告知与约束）。 |
| **中** | F-3 | `resolve_media_to_content_item`（`recognize_image`、下一轮多模态注入等）在会话路径不存在时，会回退到**项目根**或 `user_file/` 解析相对路径，可能弱化「仅工作区」直觉（与 `read_file` 策略不完全一致）。 |
| **中** | F-4 | Unix socket IPC（`AutomationIPCServer`）对 socket 使用 `0o666`，且协议层**无**应用级身份认证；任何能连上该 socket 的本机主体可调用 JSON-RPC 方法（含 `run_turn_stream`），信任边界为 **OS 对 socket 路径的访问控制**。 |
| **中** | F-5 | `readable_roots.json` / `writable_roots.json` 无条目数或总长度上限；恶意或误操作可放大 JSON 解析与每次工具调用的前缀枚举成本（可用性与轻微 DoS）。 |
| **低** | F-6 | 路径检查普遍使用 `Path.resolve()` + `relative_to(allowed_root)`；对 race 条件（检查与打开之间目标被替换为指向保护区外的 symlink）未做 `O_NOFOLLOW` 类硬防护（多数 Agent 工具链同类限制）。 |
| **低** | F-7 | `BashSecurity` 对危险命令的检测为启发式正则，无法保证完备；依赖 OS 用户隔离与人工审批补偿。 |

---

## 2. 信任边界与威胁模型

### 2.1 主要信任边界

1. **终端用户**：通过 CLI / 飞书 / 其它前端与 Agent 交互；可诱导模型调用工具。
2. **LLM**：不可信；可发出任意工具名与参数（受工具 schema 与内核校验约束）。
3. **automation daemon 进程**：持有配置文件、API 密钥、`.env`、项目与 `data/` 的访问能力；为强信任根。
4. **同机其它进程**：若可连接 automation Unix socket 或读写 `data/acl/`，则边界被削弱。

### 2.2 典型威胁

- **机密性**：通过文件类工具或附件路径读取宿主机敏感文件。
- **完整性**：通过 bash / `write_file` / `memory_store` 等修改工作区或白名单路径外资源。
- **可用性**：拖垮 daemon、撑爆 ACL JSON、或滥用网络/子进程工具。

---

## 3. 身份与执行上下文

### 3.1 `__execution_context__`

在 `AgentKernel` 处理 `ToolCallAction` 时注入（节选语义）：

- `profile_mode`：`full` | `sub` | `background`
- `allow_dangerous_commands`：是否允许配置层面的「危险命令」路径（仍受 `BashSecurity` 与审批约束）
- `bash_workspace_admin`：为 `True` 时跳过按用户单元格的 `~` 语义与部分隔离策略（与 `command_tools` 中管理员 memory owner 配置一致）
- `source`、`user_id`、`session_id`：租户命名空间与 ACL 键

### 3.2 `CoreProfile`（`agent_core/kernel_interface/profile.py`）

- **`allowed_tools` / `deny_tools`**：执行前强制校验；`deny_tools` 优先。
- **始终允许的核心工具**：`search_tools`、`call_tool`、`bash`、`request_permission`、`ask_user`（即使不在白名单内）。
- **`mode=sub`**：`modify_file` / `write_file` 等在 `file_tools` 内被禁止（`_sub_mode_forbids_file_mutation`）；bash 走最严白名单与无 shell 运算符策略。
- **`bash_workspace_admin`**：等价「工作区管理员」能力，可绕过租户级 `read_file` 根集合（见 `file_tools._resolve_read_path_for_file_tool`）。

---

## 4. 文件与路径：控制矩阵

### 4.1 配置开关（`command_tools` / `file_tools`）

| 配置项 | 作用 |
|--------|------|
| `file_tools.allow_read` / `allow_write` / `allow_modify` | 全局关闭对应工具能力 |
| `command_tools.workspace_isolation_enabled` | 开启租户工作区、`~` 重映射、bash 笼、`read_file` 读根限制等 |
| `command_tools.acl_base_dir` | `readable_roots.json`、`writable_roots.json` 根目录 |
| `command_tools.bash_os_user_enabled` + Linux `runuser` | bash 以独立 Linux 用户运行（需部署配合，见 `deploy/README.md`） |

### 4.2 `read_file`（`system/tools/file_tools.py`）

- **未隔离**：解析后路径受 `file_tools.base_dir` 等既有逻辑约束（见工具内注释与历史行为）。
- **隔离且非 `bash_workspace_admin`**：解析路径必须落在  
  `workspace_root` ∪ `tmp_root` ∪ `merged_bash_write_root_paths`（含 canonical memory）∪ `load_user_readable_prefixes` ∪ `list_ephemeral_readable_prefixes`  
  之一的路径前缀下（`_is_path_within_root`）。

### 4.3 `write_file` / `modify_file`

- **隔离**：写入路径必须在 `workspace_root`、`tmp_root` 与 `merged_bash_write_root_paths` 允许的集合内。
- **`sub` mode**：一律禁止文件变更类工具。

### 4.4 路径注入辅助（`agent_core/agent/tool_path_resolution.py`）

- `apply_workspace_path_resolution_to_tool_args` 在 kernel 内对特定工具的字符串参数预解析为绝对路径，包括：`read_file` / `write_file` / `modify_file`、`attach_media`、`attach_image_to_reply`、`memory_ingest` 等。
- **注意**：预解析不等于读权限校验；`memory_ingest` 仍由下游 `ContentMemory.ingest_file` 直接读盘（见 F-1）。

---

## 5. Bash 与子进程

### 5.1 `BashSecurity`（`agent_core/bash_security.py`）

- **Layer 1**：sub 模式白名单 + 禁止 `|;&` 等 shell 运算符（防逃逸）。
- **Layer 2**：危险模式正则（`rm -rf`、`sudo`、`curl|sh` 等）。
- **Layer 3**：需 `request_permission` + `consume_bash_danger_grant` 的一次性批准。
- **工作区笼**：检测写路径是否越出 `workspace_jail` / tmp / extra write roots；禁止 `builtin cd` / `command cd` 等绕过。

### 5.2 `BashRuntime`（`agent_core/bash_runtime.py`）

- 使用 `asyncio.create_subprocess_exec`；可选用**非继承**的 `subprocess_env`（runuser 场景），减少向租户泄露 daemon 环境变量。

### 5.3 残余风险

- 启发式规则无法覆盖所有 unix 命令组合；**`bash_os_user_enabled`** 将破坏面限制在目标 Linux 用户的权限域内，强烈建议生产隔离场景启用并正确配属主与目录权限。

---

## 6. 人类审批与 ACL

### 6.1 `request_permission`（`agent_core/tools/request_permission_tool.py`）

- 通过 `wait_registry` 阻塞等待前端 `resolve_permission`。
- 批准后按 `kind` 与 `persist_acl`：写入 `readable_roots.json` / `writable_roots.json`，或调用 ephemeral grants（仅进程生命周期）。

### 6.2 路径前缀推断（`agent_core/tools/permission_path_infer.py`）

- 从 `details` JSON 的 `path_prefix`、`path`、`target_path` 等推断前缀；经 `expand_user_path_str_for_session` 与 `resolve` 规范化。
- **依赖人类审核卡片内容**；若 UI 展示的前缀与 LLM 填写的 `details` 不一致，应以人类确认为准（设计已强调 persist 仅由人类选择）。

### 6.3 持久化存储

- 路径：`{acl_base_dir}/{frontend}/{user_id}/readable_roots.json`（writable 同理）。
- **完整性**：应保证仅 daemon 可写、备份与版本控制策略明确；被篡改时等价于扩大租户读写范围（F-5）。

---

## 7. IPC 与多前端

### 7.1 `AutomationIPCServer`（`system/automation/ipc.py`）

- Unix domain socket；`chmod(0o666)` 以便非 root 前端连接 root daemon（runuser 场景）。
- 每行 JSON 请求，无 TLS/无 token：**安全等价为「能访问该 socket 路径的本地 UID」均可驱动会话**（F-4）。
- **建议**：生产使用目录权限 + 专用 unix 组、或抽象套接字 + 文件系统 ACL；或增加应用层 shared secret / 对等认证。

### 7.2 飞书权限卡

- `resolve_permission` 经 IPC 在 daemon 内执行，避免仅网关进程持有审批状态的不一致。

---

## 8. 工具级逐项摘要

以下仅列与**本地资源访问**或**高影响动作**强相关的工具；完整列表以运行时 registry 为准。

| 工具 / 模块 | 本地读 | 本地写 | 网络 / 子进程 | 与隔离策略一致性 |
|-------------|--------|--------|----------------|------------------|
| `read_file` | 是（隔离下强校验） | 否 | 否 | 一致 |
| `write_file` / `modify_file` | 否 | 是（隔离下校验） | 否 | 一致 |
| `bash` | 由 shell 与文件系统语义决定 | 同左 | 是 | 与 `BashSecurity` + 可选 runuser 一致 |
| `memory_ingest` | **任意存在路径**（F-1） | 写入内容记忆目录 | 可能 markitdown 子进程 | **不一致，高风险** |
| `memory_store` | 否 | 内容记忆目录内 | 否 | 路径由实现约束，不暴露任意读 |
| `load_skill` | 会话技能目录下 SKILL | 否 | 否 | 与 session_paths 一致 |
| `attach_media` | 运行时下一轮读 | 否（仅登记路径） | 否 | 路径经 kernel 预解析 |
| `attach_image_to_reply` / `attach_file_to_reply` | 打开前仅 resolve + exists（F-2） | 否 | URL 模式走 http | **读边界弱于 read_file** |
| `recognize_image` | 经 `resolve_media_to_content_item`（F-3） | 否 | 是（VL API） | 与 read_file 部分重叠、回退逻辑更宽 |
| `extract_web_content` / `web_search` | 否 | 否 | 是 | 外网 SSRF 类风险依赖 MCP/Tavily 侧 |
| `get_automation_activity` | 固定 `automation_activity.jsonl` | 否 | 否 | 低敏感聚合日志 |
| `sync_sources` 等 automation | 依 runtime | 依 runtime | 是 | 需单独数据分类 |

---

## 9. 建议的修复与加固顺序

1. **P0**：为 `memory_ingest`（及直接读盘的 `ContentMemory.ingest_file` 调用方）增加与 `read_file` 相同的允许根校验（或复用 `_resolve_read_path_for_file_tool` 的只读语义）；sub 模式与隔离模式需有测试用例。
2. **P0/P1**：为 `attach_image_to_reply` / `attach_file_to_reply` 的本地路径增加与「对外发送」风险等级匹配的校验（至少：隔离下与 `read_file` 同集合，或显式二次 `request_permission`）。
3. **P1**：收紧 `_resolve_media_path` 的回退逻辑，或在文档中明确「相对路径解析顺序」，避免与 `read_file` 安全叙事冲突。
4. **P1**：IPC 认证或文件系统级收紧（见 F-4）；至少在生产文档中标注「socket 等同 root 等价物」。
5. **P2**：ACL JSON 条目上限、日志审计（批准人、前缀、时间）、可选 symlink 硬化（`open(..., O_NOFOLLOW)` 等，平台相关）。

---

## 10. 参考源码位置（便于复核）

| 主题 | 路径 |
|------|------|
| 文件工具读/写校验 | `src/system/tools/file_tools.py` |
| 路径解析与 kernel 注入 | `src/agent_core/agent/tool_path_resolution.py`、`src/system/kernel/kernel.py` |
| 审批与 ACL | `src/agent_core/tools/request_permission_tool.py`、`readable_roots_store.py`、`writable_roots_store.py`、`readable_ephemeral_grants.py` |
| Bash 安全 | `src/agent_core/bash_security.py`、`src/agent_core/tools/bash_tool.py` |
| OS 用户隔离 | `src/agent_core/bash_os_user.py`、`src/agent_core/bash_runtime.py` |
| 内容记忆任意读 | `src/agent_core/memory/content_memory.py`、`src/system/tools/memory_tools.py` |
| 媒体路径 | `src/agent_core/utils/media.py`、`src/system/tools/media_tools.py` |
| IPC | `src/system/automation/ipc.py` |
| Core 权限模型 | `src/agent_core/kernel_interface/profile.py` |

---

## 11. 免责声明

本报告基于当前仓库快照的静态分析，不构成法律或合规认证结论；部署环境（容器、systemd、文件系统权限、网络策略）会显著改变实际风险，需结合运维配置单独评估。
