# 把 macchiatoBot 接进 DSH Harness：部署与踩坑指南

> 面向给 mbot 上游提交这块能力。所有结论都来自实际部署（DSH 0.1.5 + macchiatoBot，
> Ubuntu 24.04），文中标注了哪些是**必须改上游代码**、哪些是**部署时要改配置**。
>
> 一句话：**这不是「装个插件」的事**，DSH 与 mbot 的模型差异（回合/步的生命周期、
> 审批、后台任务、会话过期）必须在两边各补一半，否则会出现「回合跑到一半突然停」
> 「界面一直转圈」「任务永远不结束」这类现象。

---

## 0. 它解决什么问题

mbot 是常驻 daemon + 自有 IPC + 自有前端（飞书/水源），DSH 是另一套 agent 宿主。
两边要合在一起，需要一层**桥**：把 DSH 的「一步」翻译成 mbot 的一次 `run_turn_stream`，
再把 mbot 的流式事件翻译回 DSH 的会话事件。

桥站在两侧的假设不重合处，需要显式处理：

| 差异 | DSH 假设 | mbot 假设 | 不处理的后果 |
|---|---|---|---|
| 单步时长 | 一步 = 一次模型调用 + 工具，随时可发事件 | 一次流可能十分钟没消息（工具在跑），只有 15s 心跳 | 判超时 → 误杀长工具 |
| 审批 | 有 `approval` 服务，必须返回 4 种 outcome | 前端收到 `feishu_permission_notify` 自己弹卡片 | 审批永远不回来 |
| 后台任务 | 不知道有 `job` 概念 | bash 超时会自动转后台，结果无处可去 | 任务跑完无人知道 |
| 会话过期 | 不关心 | 内核按 TTL 淘汰（默认 1800s） | 空闲半小时后上下文失效 |
| 轮次上限 | 由 agent loop 决定 | `max_iterations` 默认 **10** | 长任务第 10 步被截断 |

---

## 1. 部署拓扑

```
                     ┌──────────────────────────── 浏览器 ────────────────────────────┐
                     │  DSH Web (token 登录)                                          │
                     └───────────────────┬────────────────────────────────────────────┘
                                  HTTPS  │  nginx (lab.drewjin.sh)
                                         │  Host 必须透传 $http_host
                     ┌───────────────────▼────────────────────────────────────────────┐
                     │  dsh-lab.service   DSH 0.1.5 (--profile lab web)  :18766       │
                     │   └─ 插件 @lab/dsh-agent-mbot（桥）                            │
                     └───────────────────┬────────────────────────────────────────────┘
                        unix socket      │  run_turn_stream / tool_exec / resolve_permission
                     ┌───────────────────▼────────────────────────────────────────────┐
                     │  macchiato-automation.service   mbot daemon                    │
                     │   automation.sock · 模型 · 工具 · 记忆 · 定时任务              │
                     └───────────────────┬────────────────────────────────────────────┘
                                         │  worker 反向连接（WS）
                     ┌───────────────────▼────────────────────────────────────────────┐
                     │  远端机器 worker（gpu / 实验机）· 通过 unix socket 暴露文件通道 │
                     └────────────────────────────────────────────────────────────────┘
```

三个 systemd 单元：`dsh-lab`、`macchiato-automation`、`lab-pty`（文件通道桥，供 FUSE 挂载用）。

---

## 2. 必改的上游配置（不改进不了）

这一节是**最容易踩、最难查**的部分：都是「默认值看起来合理，实际会截断任务」。

### 2.1 `agent.max_iterations` 默认 10

一次回合最多 10 次模型调用。任何多步任务（12 次工具调用、跨文件重构）都会在第 10 步
被截断，前端表现为「任务跑到一半停了」。

```yaml
agent:
  max_iterations: 300        # 作者自用值；本项目按需开到 1000
```

> 现场证据：旧配置下跑「分 12 次调用 bash」的用例，第 10 次就结束了。改后 12 次全部执行。
> **建议上游把默认值提到 300**，10 对 agent 宿主来说没有意义。

### 2.2 `agent.session_expired_seconds` 在代码里根本没有这个字段

配置里写 `agent.session_expired_seconds: 14400` **不会生效**——`AgentConfig` 没有声明该字段，
`getattr(cfg.agent, "session_expired_seconds", 1800)` 永远拿到硬编码的 1800。
同时 `checkpoint.json` 里的 `remaining_ttl_seconds` **优先于配置**：

```python
session_ttl = ckpt.remaining_ttl_seconds or float(
    getattr(self._config.agent, "session_expired_seconds", 1800))
```

于是无论怎么改配置，恢复出来的会话仍按 30 分钟过期。**必须两处都改**：

1. `AgentConfig` 增加字段（并给合理默认值）；
2. 迁移 checkpoint 里的 `remaining_ttl_seconds` 与 `core_profile.session_expired_seconds`。

```yaml
agent:
  session_expired_seconds: 604800   # 7 天；忙碌时一天要重开会话好几次
```

### 2.3 其它默认值太紧

| 项 | 默认 | 建议 | 原因 |
|---|---|---|---|
| `llm.request_timeout_seconds` | 120 | 300 | 大上下文首 token 慢，120s 直接超时 |
| `memory.max_working_tokens` | 60000 | 500000 | 工作记忆过早压缩，丢上下文 |
| `agent.subagent_max_iterations` | 15 | 200 | 子 agent 一超就被砍 |
| `agent.subagent_max_seconds` | 600 | 900 | 同上 |
| `agent.subagent_max_tokens` | 500000 | null | 同上 |
| `agent.working_set_size` | 6 | 30 | 工作集太小 |

`llm.max_tokens` 保持不动：要按 provider 的真实上限设，盲目调大会被 API 拒。

---

## 3. 必须修的上游 bug（会表现为「突然停止」）

### 3.1 命令里有未配对引号 → 整个回合崩

```
bash_security._collect_workspace_write_grants → shlex.split → ValueError: No closing quotation
→ Tool 'bash' raised an unhandled exception → 回合中断
```

`shlex.split` 在不配对引号时抛异常，这条路径没有 try。**修法**：`_split_command_segments` /
`_tokenize_segment` 捕获异常并返回 `[]`（保守：不授予任何写权限）。

### 3.2 远程 worker 断连 → 整个回合崩

```
RuntimeError: remote worker disconnected
  at remote/worker_registry.execute_command
  at tools/bash_tool._execute_remote_sync
→ Tool 'bash' raised an unhandled exception
```

`_remote_exec_with_reopen` 第一次失败会 `raise`，而**重连成功后那一记重试没有 try**，
异常直接冒泡。**修法**：在 `_execute_remote_sync` 里兜住，转成可重试的工具错误：

```python
return ToolResult(success=False, error="REMOTE_WORKER_UNAVAILABLE",
                  message="远程机器 X 当前不可达（连接中断或正在重连）…",
                  data={"retryable": True})
```

> 通用原则：**工具侧任何瞬时故障都不该终止回合**，应该变成 agent 能读懂、能重试的错误。

### 3.3 heredoc 命令返回 rc=2 且输出为空

`job_manager` 把命令包成 `f"({command}) > {log} 2>&1"`，命令以 `<<'EOF'` 结尾时和右括号
粘连成 `EOF)`，shell 直接语法错。**修法**：包装前保证命令以换行结尾。

### 3.4 `chat_search` 查带连字符的词直接 SQL 报错

FTS5 把 `gpu-worker` 解析成 `gpu - worker`。**修法**：每个词整体加引号再拼查询。

### 3.5 失效 token 的 worker 把日志刷爆

旧机器用废 token 每 4 秒重试，实测 **3 小时 2508 条** WARNING。**修法**：同
`(login, reason)` 10 分钟只打一条，并带出期间被压制的次数。

---

## 4. 桥本身的实现要点

这些是插件（DSH 侧）必须处理的，直接抄结论即可。

### 4.1 超时预算要按阶段分

mbot 的工具执行期只发心跳、没有内容事件。若把「心跳」当进展，卡死就永远判不出来；
若把「长时间无内容」直接当卡死，长工具就被误杀。正确做法：

```js
idleMs: () => (pendingTools > 0 ? config.toolMs : config.stallMs)
stallMs = 600_000   // 等模型：实测有 307s 的合法步骤
toolMs  = 1800_000  // 等工具：只靠心跳，不能按等模型的尺子量
```

心跳日志要带上当前上限，出问题时能直接看出「还要等多久」。

### 4.2 审批要走 DSH 的 approval 服务，而不是自制卡片

```js
const approval = ctx.approval ?? ctx.reflect.get("approval", false);
const outcome = await approval.request({ agent, toolName, reason, signal });
// → allowed-once | rejected | cancelled | unavailable
```

`ctx.approval` 直接取会抛 `cannot get property "approval" without inject`，必须用
`ctx.reflect.get(..., false)`。四种 outcome 要映射到 mbot 的
`resolve_permission {permission_id, allowed, note}`，并且**另开一条连接**发送（不要占用回合流）。

### 4.3 后台任务完成要能把会话唤醒

mbot 的 bash 超时会自动转后台并返回 `job_id`。桥要：

1. 从工具结果里识别（真实 payload 形如 `{"job_id": "...", "status": "running"}`，也可能包一层 `data`）；
2. 每 10s 用 `tool_exec {tool:"bash", args:{job_status}}` 轮询到终态；
3. `agent.followup(message)` —— 它会走 `send(msg, "next-turn", true) → wakeDriver()`，
   **空闲时会直接开一个新回合**，不是排队等用户说话。

> 注意 `tool_exec` 必须带 `client_id`，否则 IPC 拒绝。

### 4.4 工具事件必须按真实时刻落库

DSH 面板的「工具调用用时」是 `node.time - node.callTime`（结果事件时间 − 调用事件时间）。
如果桥把 `tool/call` 和 `tool/result` 都攒到一步结束后统一 append，两者时间戳只差几毫秒，
**工具耗时永远显示 0 秒**。做法：

1. 遇到第一个工具事件前，先把已积累的正文落成 `assistant/message`（保住「正文 → 卡片」顺序）；
2. `tool-call` 到达即 append `tool/call` 并记住 seq；
3. `tool-result` 到达即 append `tool/result`，用 `sourceEventSeqs` 引用 call。

### 4.5 会话流式增量要真的转发

桥如果只在一步结束时一次性推送全文，前端看不到流式过程，且 DSH 的 TPS 会算出离谱值
（它用「输出 token ÷ 首末 chunk 到达差」）。DSH 面板的 TPS 本身也不准：模型先思考
（reasoning token 发生在首包之前）再吐字，实测 458 token 里 298 是 reasoning，
真实速度约 194 tok/s 而面板会显示两千多。

---

## 5. DSH 侧的部署坑

### 5.1 页面需要 token，且 token 默认每次重启都变

`dsh` 启动时用 `randomBytes` 生成一个 launch token 并拼进 URL（`?token=...`），
**进程重启就换一个**，所以每次重启都要重新拿链接。要让某台机器长期可访问：

- 固定 token：让 DSH 从环境变量读（本项目给 `dsh-client-connection` 打了补丁读 `DSH_WEB_TOKEN`）；
- 固定对外 URL：打印的地址用 `DSH_WEB_PUBLIC_URL`（否则反代后打印的是内部端口）；
- 延长 cookie：`cookieMaxAgeDays: 365`，否则手机端反复登录。

> 这两个都是**给 dsh 本体打的小补丁**（`patch-dsh-web-url.mjs`），升级 DSH 后需要重放。

### 5.2 `--trusted-host` 在 `--profile` 模式下不生效

现象：页面一直「重连中」，`/api/remote.mux` 返回 403。

0.1.5 的 `--trusted-host` 只在非 profile 模式生效；profile 模式下要把白名单写进插件层：

```yaml
# cordis.patch.yml
- id: dsh-client-connection
  config:
    connection:
      trustedHosts: ["lab.example.com", "127.0.0.1:18766"]
```

### 5.3 反向代理要透传 Host

nginx 用 `proxy_set_header Host $http_host`（**含端口**）。只写 `$host` 会把端口丢掉，
DSH 的 Origin 校验失败 → 同样是 403。

### 5.4 品牌可替换

DSH 是 slot 架构，界面 logo/字标可以通过 `sidebar.brand.mark`、`sidebar.brand.name`、
`conversation.hero.brand.mark` 三个 slot 注入。**必须同时把官方品牌插件
`@deepseek-ai/dsh-client-ui-brand-official` 设为 `disabled: true`**，否则两套组件叠加。
favicon 与 `<title>` 是静态资源，直接替换 `dsh-web-frontend/dist/{favicon.svg,index.html}`，
运行时再补一个 `MutationObserver` 钉住（应用会在路由切换时改回）。

---

## 6. 文件通道 / 远程挂载

如果还想在本地用 nvim 编辑远端文件：

- 远端 worker 起一个 unix socket，服务端桥把它转成 `/pty`（终端）和 `/fs`（文件协议）；
- 本地用 FUSE 挂到 `~/mnt/<机器>/<项目>`；
- **挂载单元不要写 `Requires=<桥>`**：重启桥会连带停掉挂载。用 `Wants=` + `After=`，
  并给挂载进程做**进程内重连**（指数退避 ~10s），否则一次桥重启要等 systemd 重新拉起。
- 挂载单元的 `RestartSec` 不要设太大：离线机器降噪到 5 分钟是合理的，但在线的机器
  一旦失败，5 分钟不恢复。折中：`RestartSec=300` + `ExecStartPre` 等待 socket 就绪。

---

## 7. 部署步骤（从零复现）

```bash
# 1) 装 DSH（本项目的版本是 0.1.5-rc.1）
npm i -g @deepseek-ai/dsh@0.1.5-rc.1

# 2) 从内置 web profile 派生一个自己的
dsh --profile lab --from-default-profile web
#    之后用 `dsh --profile lab web` 启动（0.1.5 的 `dsh web` 不接受 --profile）

# 3) 装桥插件
cd dsh-agent-mbot
node scripts/vendor.mjs          # 从本机 DSH 取 dsh-agent-loop 并打补丁
node scripts/install.mjs lab     # 拷进 profile 的 node_modules + 挂 bundle 层 + 重放 web-url 补丁

# 4) 固定 token / 对外地址（否则每次重启换 token、打印的 URL 是内网端口）
sudo systemctl edit dsh-lab
#    Environment=DSH_WEB_TOKEN=<随机串>
#    Environment=DSH_WEB_PUBLIC_URL=https://lab.example.com

# 5) nginx：Host 透传（含端口）+ /pty /fs 反代
# 6) 自检
node scripts/smoke-{model,fs,stall,approval,jobwatch}.mjs
```

> `install.mjs` 刻意**不走 pnpm**：插件依赖的 `@deepseek-ai/*` 必须和正在运行的 DSH 是
> 同一份实例，否则 `session.append`、`AssistantStreamAttempt` 这些结构体对不上。它把包直接放进
> profile 的 `node_modules`，Node 会沿目录向上找到 `~/.dsh/profiles/node_modules/@deepseek-ai/*`。

### 验证清单

| 项 | 怎么验 |
|---|---|
| 回合能跑通 | 发一条消息，看 `turn/end` 的 reason 是 `completed` |
| 长工具不误杀 | 让它 `sleep 210`，应正常返回而不是 `MBOT_STALLED` |
| 审批闭环 | 写工作区外的文件，DSH 里应弹卡片，允许/拒绝两条路都要试 |
| 后台任务 | 让它 `background: true` 跑 45s，**关掉页面**，回来应看到自动汇报 |
| 工具耗时 | 跑一次 `sleep 6`，面板「工具调用用时」应 ≈6s 而不是 0 |
| 挂载自愈 | `systemctl restart` 桥，挂载点应仍可读、不重新挂载 |
| 会话不早退 | 空闲 30 分钟后继续对话，上下文应还在 |

---

## 8. 常见现象 → 原因对照

| 现象 | 原因 | 修法 |
|---|---|---|
| 任务跑到一半停 | `max_iterations` 太小 | 调到 300+ |
| 空闲半小时后上下文没了 | checkpoint 的 `remaining_ttl_seconds` 优先 | 两处一起改 |
| 长命令被杀（180s） | 工具期只有心跳，被判无进展 | 分阶段超时 + 心跳不计入 |
| 回合报 `unhandled exception` | shlex 引号 / worker 断连 | 捕获并转成工具错误 |
| 审批永远不回来 | 只发了通知，没接 approval 服务 | `ctx.reflect.get("approval")` |
| 页面一直重连中 | trustedHosts / Host 丢了端口 | 写进 profile 配置 + `$http_host` |
| 手机反复登录 | cookie 30 天 + token 每次变 | 固定 token + 365 天 |
| heredoc 返回空 | 包装命令没补换行 | 包装前加 `\n` |
| 工具耗时显示 0 秒 | 事件攒到步末才落库 | 按真实时刻落库 |
| TPS 两千多 | 分母只算首末 chunk | 属显示问题；要准需把 reasoning 时序也报上去 |

---

## 9. 提交建议

建议拆成 4 个 PR：

1. **fix(bash_security)**: shlex 引号不配对被捕获
2. **fix(bash_tool)**: 远程 worker 断连返回可重试错误
3. **fix(config)**: `max_iterations` 默认 10 → 300；补 `session_expired_seconds` 字段
4. **feat**: 默认配置里的超时/内存项对齐（`request_timeout_seconds`、`max_working_tokens`、
   subagent 三项）

桥插件本身（`dsh-agent-mbot`）可以作为独立仓库贡献，附上面的部署文档与 5 个 smoke 用例。

---

*本文档中的每条结论都有对应的复现步骤与回归测试；测试用例在 `macchiatoBot/tests/` 与
`dsh-agent-mbot/scripts/smoke-*.mjs`。*
