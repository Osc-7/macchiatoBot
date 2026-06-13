#!/usr/bin/env python3
"""Long-running automation daemon.

Responsibilities:
1. Run scheduler + queue consumer for background automation jobs.
2. Expose local IPC for CLI / other frontends.
3. Centralize session expiration checks inside automation process.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from agent_core import AgentCore, CoreSessionAdapter
from agent_core.bash_os_user import reconcile_admin_linux_users
from agent_core.config import get_config
from agent_core.interfaces import AgentHooks
from agent_core.llm.client import LLMClient
from agent_core.tools.bash_job_notify import (
    deliver_via_inject,
    format_notification,
    poll_terminal_jobs,
    set_notify_dependencies,
)
from frontend.feishu.ask_user_notify import install_feishu_ask_user_notify_hook
from frontend.feishu.client import FeishuClient
from frontend.feishu.permission_notify import install_feishu_permission_notify_hook
from frontend.feishu.reply_dispatch import send_feishu_agent_reply
from system.automation import (
    AgentTaskQueue,
    AutomationCoreGateway,
    AutomationIPCServer,
    AutomationScheduler,
    IPCServerPolicy,
    SessionCutPolicy,
    SessionRegistry,
    default_socket_path,
)
from system.automation.agent_task import TaskStatus
from system.automation.config_sync import sync_job_definitions_from_config
from system.automation.logging_utils import AutomationTaskLogger
from system.automation.remote_worker_server import (
    remote_server_enabled,
    run_remote_worker_server_until_stopped,
)
from system.automation.repositories import JobDefinitionRepository, JobRunRepository
from system.kernel import (
    AgentKernel,
    CorePool,
    CoreProfile,
    KernelRequest,
    KernelScheduler,
    KernelTerminal,
    SessionSummarizer,
)
from system.tools import build_tool_registry, get_default_tools


def _repo_root() -> Path:
    """Repository root when running from checkout; else current working directory."""
    here = Path(__file__).resolve().parent
    if here.parent.name == "src":
        return here.parent.parent
    return Path.cwd()


LOG_DIR = _repo_root() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "automation_daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("automation_daemon")

POLL_INTERVAL_SECONDS = 5
# 单条自动化任务超过此时长打 WARNING，便于发现「卡住整条队列」的长任务
_CONSUME_SLOW_SECONDS = 300.0

# 无 memory_owner 的定时任务共用这个 user_id 段，对应目录如
# data/workspace/cron/_automation/ ，避免每个 job 名一层 cron/job-config-xxxx/。
# 若需与用户飞书目录一致或任务间磁盘隔离，请为 job 配置 memory_owner。
_AUTOMATION_SHARED_WORKSPACE_USER_ID = "_automation"


def _instruction_preview(text: str, *, max_len: int = 120) -> str:
    t = (text or "").replace("\n", " ").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _workspace_frontend_user_for_automation_task(
    *,
    raw_owner: str,
    task_source: str,
    task_user_id: str,
) -> tuple[str, str]:
    """
    解析 CoreProfile.frontend_id / dialog_window_id。

    须满足 memory_paths.validate_logic_namespace_segment（段内禁止 ':'、'/'）。
    无 memory_owner 时 task.source 为 ``cron:{job_name}``，不能整段作为 frontend（否则会触发
    ensure_workspace_owner_layout 校验失败）。此时工作区固定为 ``cron/_automation``，不按 job 名分目录。
    """
    ro = (raw_owner or "").strip()
    if ro and ":" in ro:
        ms, mu = ro.split(":", 1)
        ms, mu = ms.strip(), mu.strip()
        uid = (mu or task_user_id or "default").strip() or "default"
        return ms, uid
    if ro:
        uid = (task_user_id or "default").strip() or "default"
        return ro, uid

    ts = (task_source or "").strip()
    if ts.lower().startswith("cron:"):
        return "cron", _AUTOMATION_SHARED_WORKSPACE_USER_ID
    if ":" in ts:
        return ts.replace(":", "_"), (task_user_id or "default").strip() or "default"
    return ts or "cli", (task_user_id or "default").strip() or "default"


def _resolve_feishu_chat_id_for_automation_task(
    *,
    raw_owner: str,
    explicit_chat_id: str,
    core_pool: CorePool,
) -> str:
    """优先使用任务显式 chat_id，否则尝试从 feishu memory_owner 回溯最近会话 chat_id。"""
    cid = (explicit_chat_id or "").strip()
    if cid:
        return cid
    owner = (raw_owner or "").strip()
    if not owner.startswith("feishu:"):
        return ""
    owner_id = owner.split(":", 1)[1].strip() if ":" in owner else ""
    if not owner_id:
        return ""
    try:
        from frontend.feishu.feishu_turn_hooks import resolve_feishu_chat_id_for_session

        for sid in (f"feishu:user:{owner_id}", f"feishu:chat:{owner_id}"):
            resolved = resolve_feishu_chat_id_for_session(sid, core_pool=core_pool)
            if resolved:
                return str(resolved).strip()
    except Exception:
        return ""
    return ""


async def _consume_loop(
    queue: AgentTaskQueue,
    scheduler: KernelScheduler,
    stop_event: asyncio.Event,
) -> None:
    logger.info(
        "consume: consumer loop started, poll_interval=%ss", POLL_INTERVAL_SECONDS
    )
    _idle_polls = 0
    _ALIVE_LOG_INTERVAL = 120  # 每 120 次空轮询(~10min)打一次存活日志
    while not stop_event.is_set():
        try:
            task = queue.pop_pending()
        except Exception:
            logger.exception(
                "consume: pop_pending() raised, will retry in %ss",
                POLL_INTERVAL_SECONDS,
            )
            task = None
        if task is None:
            _idle_polls += 1
            if _idle_polls % _ALIVE_LOG_INTERVAL == 0:
                logger.info(
                    "consume: alive (idle polls=%d, pending=%d)",
                    _idle_polls,
                    queue.pending_count(),
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        _idle_polls = 0
        started = time.perf_counter()
        pending_behind = queue.pending_count()
        running_in_db = queue.running_count()
        task_logger = AutomationTaskLogger(task)
        task_logger.log_task_start()
        logger.info(
            "consume: popped task_id=%s source=%s session_id=%s pending_behind=%d "
            "running_in_db=%d instruction_preview=%r",
            task.task_id,
            task.source,
            task.session_id,
            pending_behind,
            running_in_db,
            _instruction_preview(task.instruction),
        )
        activity_record: dict[str, Any] | None = None
        outcome = "interrupted"
        remote_bound = False
        remote_login = ""
        try:
            if isinstance(task.metadata, dict):
                remote_login = str(task.metadata.get("remote_login") or "").strip()
                if remote_login:
                    from agent_core.remote.worker_registry import (
                        get_remote_worker_registry,
                    )
                    from agent_core.remote.workspace_state import (
                        activate_remote_workspace,
                    )

                    remote_path = (
                        str(task.metadata.get("remote_path") or "~").strip() or "~"
                    )
                    remote_profile = (
                        str(task.metadata.get("remote_profile") or "dev").strip()
                        or "dev"
                    )
                    if remote_profile not in {
                        "strict",
                        "dev",
                        "host-user",
                        "host-admin",
                    }:
                        remote_profile = "dev"
                    ttl_raw = task.metadata.get("remote_ttl_seconds")
                    remote_ttl_seconds: int | None = None
                    if ttl_raw is not None:
                        try:
                            remote_ttl_seconds = int(ttl_raw)
                        except (TypeError, ValueError):
                            remote_ttl_seconds = None
                    remote_required = bool(task.metadata.get("remote_required", True))
                    try:
                        opened = await get_remote_worker_registry().open_workspace(
                            login=remote_login,
                            session_id=task.session_id,
                            requested_path=remote_path,
                            profile=remote_profile,  # type: ignore[arg-type]
                        )
                        activate_remote_workspace(
                            session_id=task.session_id,
                            login=remote_login,
                            requested_path=remote_path,
                            profile=remote_profile,  # type: ignore[arg-type]
                            ttl_seconds=remote_ttl_seconds,
                            resolved_path=opened.resolved_path,
                            device_label=opened.device_label,
                        )
                        remote_bound = True
                        logger.info(
                            "consume: task_id=%s bound remote workspace login=%s path=%s profile=%s",
                            task.task_id,
                            remote_login,
                            remote_path,
                            remote_profile,
                        )
                    except Exception as exc:
                        if remote_required:
                            raise RuntimeError(
                                f"绑定远程工作区失败（login={remote_login}, path={remote_path}）: {exc}"
                            ) from exc
                        logger.warning(
                            "consume: task_id=%s remote workspace bind failed, fallback local "
                            "(login=%s path=%s): %s",
                            task.task_id,
                            remote_login,
                            remote_path,
                            exc,
                        )

            async def on_trace_event(event: dict) -> None:
                task_logger.log_trace_event(event)

            hooks = AgentHooks(on_trace_event=on_trace_event)

            # 从任务 metadata 中读取显式 core_mode / memory_owner：
            # - core_mode: full / sub / background（兼容旧值 cron/heartbeat → background）
            # - memory_owner: 决定记忆 owner（如 feishu:uid / cli:root）
            raw_mode = ""
            raw_owner = ""
            if isinstance(task.metadata, dict):
                raw_mode = str(task.metadata.get("core_mode") or "").strip()
                raw_owner = str(task.metadata.get("memory_owner") or "").strip()

            mode = (raw_mode or "").lower()
            # 兼容老配置：cron / heartbeat 都视为 background
            if mode in ("cron", "heartbeat"):
                mode = "background"

            frontend_id, dialog_id = _workspace_frontend_user_for_automation_task(
                raw_owner=raw_owner,
                task_source=task.source,
                task_user_id=task.user_id,
            )
            tool_template = (
                str(task.metadata.get("tool_template") or "").strip()
                if isinstance(task.metadata, dict)
                else ""
            )

            if mode == "full":
                # full 模式下对齐 cli/feishu 主对话的权限策略：
                # - 是否允许 bash 命令由 config.command_tools.allow_run 决定
                # - 其余参数复用 agent 配置
                cfg = get_config()
                profile = CoreProfile.full_from_config(
                    cfg,
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                    tool_template=tool_template or "default",
                )
            elif mode == "sub":
                cfg = get_config()
                agent_cfg = getattr(cfg, "agent", None)
                sub_ctx = getattr(agent_cfg, "subagent_max_context_tokens", None)
                profile = CoreProfile.default_sub(
                    allowed_tools=None,
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                    max_context_tokens=sub_ctx,
                    tool_template=tool_template or "default",
                    tools_config=cfg.tools,
                )
            else:
                # 默认后台任务权限（定时任务 / 心跳）
                profile = CoreProfile.default_background(
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                    tool_template=tool_template or "cron",
                    tools_config=get_config().tools,
                )

            # 有 memory_owner 时，为该 Core 打开持久化记忆；否则仅使用工作记忆。
            # 注意：full_from_config 默认 memory_enabled=True，因此必须显式设置为 False。
            if raw_owner:
                profile.memory_enabled = True
            else:
                profile.memory_enabled = False
            # 必须把任务的 memory_owner 透传到 KernelRequest.metadata，供调度器解析记忆路径；
            # 仅依赖 CoreProfile 在部分边界情况下会与 job 配置不一致。
            req_meta: dict[str, Any] = {
                "source": "cron",
                "user_id": task.user_id,
                "_hooks": hooks,
            }
            if isinstance(task.metadata, dict):
                mo = str(task.metadata.get("memory_owner") or "").strip()
                if mo:
                    req_meta["memory_owner"] = mo
                cm = str(task.metadata.get("core_mode") or "").strip()
                if cm:
                    req_meta["core_mode"] = cm
                tt = str(task.metadata.get("tool_template") or "").strip()
                if tt:
                    req_meta["tool_template"] = tt
                explicit_chat_id = str(
                    task.metadata.get("feishu_chat_id") or ""
                ).strip()
                resolved_chat_id = _resolve_feishu_chat_id_for_automation_task(
                    raw_owner=mo,
                    explicit_chat_id=explicit_chat_id,
                    core_pool=scheduler.core_pool,
                )
                if resolved_chat_id:
                    req_meta["feishu_chat_id"] = resolved_chat_id
            request = KernelRequest.create(
                text=task.instruction,
                session_id=task.session_id,
                frontend_id=frontend_id,
                metadata=req_meta,
                profile=profile,
            )
            handle = await scheduler.submit(request)
            logger.info(
                "consume: awaiting kernel task_id=%s request_id=%s session_id=%s",
                task.task_id,
                handle.request_id,
                task.session_id,
            )
            run_result = await handle
            result = run_result.output_text
            op_ok, op_problems = task_logger.evaluate_required_operations()
            if op_ok:
                outcome = "success"
                queue.update_status(task.task_id, TaskStatus.SUCCESS, result=result)
                activity_record = task_logger.log_task_end(
                    status=TaskStatus.SUCCESS, result=result, error=None
                )
            else:
                outcome = "failed_validation"
                error_msg = "; ".join(op_problems)
                queue.update_status(
                    task.task_id, TaskStatus.FAILED, result=result, error=error_msg
                )
                activity_record = task_logger.log_task_end(
                    status=TaskStatus.FAILED, result=result, error=error_msg
                )
        except Exception as exc:
            outcome = "exception"
            logger.exception("Task %s failed: %s", task.task_id, exc)
            activity_record = task_logger.log_task_end(
                status=TaskStatus.FAILED, result=None, error=str(exc)
            )
            queue.update_status(task.task_id, TaskStatus.FAILED, error=str(exc))
        finally:
            if remote_bound:
                from agent_core.remote.worker_registry import get_remote_worker_registry
                from agent_core.remote.workspace_state import release_remote_workspace

                old = release_remote_workspace(task.session_id)
                if old is not None:
                    try:
                        await get_remote_worker_registry().close_workspace(
                            login=old.login,
                            session_id=task.session_id,
                        )
                    except Exception as close_exc:  # noqa: BLE001
                        logger.warning(
                            "consume: task_id=%s release remote workspace failed login=%s: %s",
                            task.task_id,
                            old.login,
                            close_exc,
                        )
            elapsed = time.perf_counter() - started
            log_fn = logger.warning if elapsed >= _CONSUME_SLOW_SECONDS else logger.info
            log_fn(
                "consume: finished task_id=%s source=%s session_id=%s outcome=%s "
                "elapsed_s=%.2f pending_behind=%d",
                task.task_id,
                task.source,
                task.session_id,
                outcome,
                elapsed,
                queue.pending_count(),
            )
            if activity_record is not None:
                try:
                    await _maybe_notify_feishu_activity(activity_record)
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to send Feishu automation activity notification: %s",
                        notify_exc,
                    )


async def _maybe_notify_feishu_activity(record: dict[str, Any]) -> None:
    """Optionally push a compact automation activity summary to Feishu.

    This mirrors the CLI's [system] automation activity line, but sends it to a configurable
    Feishu chat when enabled in config.feishu.
    """
    try:
        cfg = get_config()
    except Exception:
        return

    feishu_cfg = cfg.feishu
    enabled = bool(feishu_cfg.enabled)
    auto_enabled = bool(getattr(feishu_cfg, "automation_activity_enabled", False))
    chat_id = getattr(feishu_cfg, "automation_activity_chat_id", "") or ""

    if not (enabled and auto_enabled):
        return
    if not chat_id:
        return

    result = record.get("result") or {}
    result_msg = ""
    if isinstance(result, dict):
        msg = result.get("message") or ""
        if isinstance(msg, str):
            result_msg = msg.strip()

    ts = str(record.get("timestamp") or "")
    source = str(record.get("source") or "")
    prefix_ts = f"{ts} " if ts else ""
    if result_msg:
        text_out = f"{prefix_ts}{source} {result_msg}"
    else:
        text_out = f"{prefix_ts}{source}"

    if not text_out.strip():
        return

    http_timeout = max(float(feishu_cfg.timeout_seconds), 120.0)
    client = FeishuClient(timeout_seconds=http_timeout)
    await send_feishu_agent_reply(
        client=client,
        chat_id=chat_id,
        output_text=text_out,
        markdown_card_header_title="定时任务",
        reply_phase="final",
    )


async def _bash_job_notify_loop(
    stop_event: asyncio.Event,
    scheduler: "KernelScheduler",
    core_pool: "CorePool",
    poll_interval: float,
) -> None:
    """daemon 后台 watcher：轮询 bash job 终态并通过 inject_turn 主动通知。"""
    logger.info("bash_job_notify: watcher started poll_interval=%.1fs", poll_interval)
    try:
        while not stop_event.is_set():
            try:
                notes = await poll_terminal_jobs(max_items=20)
            except Exception as exc:
                logger.warning("bash_job_notify: poll_terminal_jobs failed: %s", exc)
                notes = []
            for note in notes:
                try:
                    text = format_notification(note)
                    await deliver_via_inject(
                        session_id=note["session_id"],
                        text=text,
                        scheduler=scheduler,
                        core_pool=core_pool,
                        note=note,
                    )
                except Exception as exc:
                    logger.warning(
                        "bash_job_notify: deliver failed session=%s job=%s: %s",
                        note.get("session_id"),
                        note.get("job_id"),
                        exc,
                    )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(0.1, poll_interval)
                )
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        logger.info("bash_job_notify: watcher cancelled")
        raise


async def _main() -> None:
    cfg = get_config()
    reconcile_admin_linux_users(cfg.command_tools)
    install_feishu_permission_notify_hook()
    install_feishu_ask_user_notify_hook()
    owner_id = (sys.argv[1].strip() if len(sys.argv) > 1 else "root") or "root"
    source = (sys.argv[2].strip() if len(sys.argv) > 2 else "cli") or "cli"
    # 工具在 daemon 进程内加载；修改工具实现/定义（如 file_tools.read_file）后需重启本 daemon 才能生效
    # 传入 owner_id 和 source 确保记忆工具指向正确的用户目录
    tools = get_default_tools(config=cfg, user_id=owner_id, source=source)
    default_session_id = f"{source}:{owner_id}"

    queue = AgentTaskQueue()
    recovered = queue.recover_stale_running()
    if recovered:
        logger.info("Recovered %d stale running tasks", recovered)

    job_def_repo = JobDefinitionRepository()
    job_run_repo = JobRunRepository()
    sync_job_definitions_from_config(config=cfg, job_def_repo=job_def_repo)
    scheduler = AutomationScheduler(
        job_def_repo=job_def_repo, job_run_repo=job_run_repo, task_queue=queue
    )

    kernel_tool_registry = build_tool_registry(config=cfg)
    kernel = AgentKernel(tool_registry=kernel_tool_registry)
    # 使用轻量模型或与主模型相同的配置，为会话结束摘要提供专用 LLM 客户端。
    # 如需单独的总结模型，可在此处通过 model_override 指定，例如 "qwen2.5-7b-instruct" 等。
    summary_llm_client = LLMClient(config=cfg)
    summarizer = SessionSummarizer(llm_client=summary_llm_client)
    core_pool = CorePool(
        config=cfg,
        kernel=kernel,
        summarizer=summarizer,
        session_logger=None,
    )
    scheduler_runtime = KernelScheduler(kernel=kernel, core_pool=core_pool)
    set_notify_dependencies(scheduler=scheduler_runtime, core_pool=core_pool)
    kernel_terminal = KernelTerminal(
        scheduler=scheduler_runtime,
        core_pool=core_pool,
        automation_scheduler=scheduler,
        agent_task_queue=queue,
    )
    stop_event = asyncio.Event()
    consumer_task = asyncio.create_task(
        _consume_loop(queue, scheduler_runtime, stop_event),
        name="automation-consumer",
    )
    remote_server_task: asyncio.Task[Any] | None = None
    if remote_server_enabled():
        remote_server_task = asyncio.create_task(
            run_remote_worker_server_until_stopped(stop_event),
            name="remote-worker-server",
        )

    # IPC core session and gateway (interactive frontends)
    async with AgentCore(
        config=cfg,
        tools=tools,
        max_iterations=cfg.agent.max_iterations,
        timezone=cfg.time.timezone,
        user_id=owner_id,
        source=source,
        session_logger=None,
        defer_mcp_connect=True,
    ) as core_agent:
        core_adapter = CoreSessionAdapter(core_agent)

        async def _session_factory(session_key: str) -> CoreSessionAdapter:
            created_agent = AgentCore(
                config=cfg,
                tools=tools,
                max_iterations=cfg.agent.max_iterations,
                timezone=cfg.time.timezone,
                user_id=owner_id,
                source=source,
                session_logger=None,
                defer_mcp_connect=True,
            )
            await created_agent.__aenter__()
            try:
                adapter = CoreSessionAdapter(created_agent)
            except BaseException:
                await created_agent.__aexit__(None, None, None)
                raise
            # 不在 factory 里调用 activate_session，由 gateway._create_session 根据
            # is_expired 状态决定 replay_messages_limit，避免全量历史被错误加载。
            return adapter

        gateway = AutomationCoreGateway(
            core_adapter,
            kernel_scheduler=scheduler_runtime,
            session_id=default_session_id,
            policy=SessionCutPolicy(
                idle_timeout_minutes=int(cfg.memory.idle_timeout_minutes or 30),
                daily_cutoff_hour=4,
            ),
            session_factory=_session_factory,
            owner_id=owner_id,
            source=source,
            session_registry=SessionRegistry(),
        )
        await gateway.activate_primary_session()

        ipc = AutomationIPCServer(
            gateway,
            owner_id=owner_id,
            source=source,
            socket_path=default_socket_path(),
            policy=IPCServerPolicy(expire_check_interval_seconds=60),
            terminal=kernel_terminal,
        )

        await scheduler_runtime.start()

        notify_enabled = bool(
            getattr(cfg.command_tools, "bash_job_notify_inject_enabled", True)
        )
        notify_poll = float(
            getattr(cfg.command_tools, "bash_job_notify_poll_seconds", 3.0) or 3.0
        )
        notify_task: asyncio.Task[Any] | None = None
        if notify_enabled:
            notify_task = asyncio.create_task(
                _bash_job_notify_loop(
                    stop_event=stop_event,
                    scheduler=scheduler_runtime,
                    core_pool=core_pool,
                    poll_interval=notify_poll,
                ),
                name="bash-job-notify-watcher",
            )

        await scheduler.start()
        await ipc.start()
        logger.info("Automation daemon started. socket=%s", ipc.socket_path)

        # MCP 在后台任务中连接；关闭必须在同一任务中执行（finally 里 close_mcp_only），
        # 否则 anyio 会报 "Attempted to exit cancel scope in a different task than it was entered in"。
        async def _mcp_lifecycle_task() -> None:
            try:
                if await core_agent.ensure_mcp_connected():
                    logger.info("MCP connected (deferred)")
                await stop_event.wait()
            except asyncio.CancelledError:
                pass
            finally:
                await core_agent.close_mcp_only()

        mcp_task = asyncio.create_task(_mcp_lifecycle_task(), name="daemon-mcp-connect")

        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        finally:
            # 先由 MCP 所在任务自行 close，再继续主流程关闭
            mcp_task.cancel()
            await asyncio.gather(mcp_task, return_exceptions=True)
            consumer_task.cancel()
            await asyncio.gather(consumer_task, return_exceptions=True)
            if remote_server_task is not None:
                remote_server_task.cancel()
                await asyncio.gather(remote_server_task, return_exceptions=True)
            if notify_task is not None:
                notify_task.cancel()
                await asyncio.gather(notify_task, return_exceptions=True)
            await ipc.stop()
            await scheduler.stop()
            await scheduler_runtime.stop()
            await core_pool.evict_all()
            await gateway.close()

    # 旧版 daemon 级 SessionLogger 已关闭，核心会话日志改由 Kernel/CoreLifecycleLogger 管理。


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _loop_exception_handler(
        loop_obj: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        # 屏蔽 anyio/mcp 在异步生成器/子任务关闭时的已知噪音：
        # RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
        message = str(context.get("message") or "")
        exc = context.get("exception")

        def _is_anyio_cancel_scope(e: Any) -> bool:
            return (
                isinstance(e, RuntimeError)
                and e is not None
                and "cancel scope" in str(e)
            )

        if (
            "an error occurred during closing of asynchronous generator" in message
            and _is_anyio_cancel_scope(exc)
        ):
            return
        # 子任务（如 MCP stdio_client 内部 task）未被 await 时，asyncio 会报 "Task exception was never retrieved"
        if "Task exception was never retrieved" in message:
            task = context.get("task")
            if task is not None and task.done() and not task.cancelled():
                try:
                    task_exc = task.exception()
                except Exception:
                    task_exc = None
                if _is_anyio_cancel_scope(task_exc):
                    return
        loop_obj.default_exception_handler(context)

    loop.set_exception_handler(_loop_exception_handler)

    def _signal_handler(*_args: object) -> None:
        if not stop_event.is_set():
            stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _signal_handler)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler)

    async def _runner() -> None:
        task = asyncio.create_task(_main())
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        loop.run_until_complete(_runner())
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            shutdown_default_executor = getattr(loop, "shutdown_default_executor", None)
            if shutdown_default_executor is not None:
                try:
                    loop.run_until_complete(shutdown_default_executor())
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    main()
