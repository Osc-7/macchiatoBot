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
from pathlib import Path
from typing import Any

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
from system.automation.config_sync import sync_job_definitions_from_config
from system.automation.agent_task import TaskStatus
from system.automation.logging_utils import AutomationTaskLogger
from system.automation.repositories import JobDefinitionRepository, JobRunRepository
from agent_core.config import get_config
from agent_core import AgentCore, CoreSessionAdapter
from agent_core.interfaces import AgentHooks
from system.kernel import (
    AgentKernel,
    CorePool,
    CoreProfile,
    KernelRequest,
    KernelScheduler,
    SessionSummarizer,
)
from agent_core.llm.client import LLMClient
from system.tools import build_tool_registry

from frontend.feishu.client import FeishuClient

from agent_core.tools import get_default_tools

LOG_DIR = Path(__file__).resolve().parent / "logs"
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


async def _consume_loop(
    queue: AgentTaskQueue,
    scheduler: KernelScheduler,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        task = queue.pop_pending()
        if task is None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()), timeout=POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
            continue
        task_logger = AutomationTaskLogger(task)
        task_logger.log_task_start()
        activity_record: dict[str, Any] | None = None
        try:

            async def on_trace_event(event: dict) -> None:
                task_logger.log_trace_event(event)

            hooks = AgentHooks(on_trace_event=on_trace_event)

            # 从任务 metadata 中读取显式 core_mode / memory_owner：
            # - core_mode: full / sub / background（兼容旧值 cron/heartbeat → background）
            # - memory_owner: 决定记忆 owner（如 feishu:uid / cli:default）
            raw_mode = ""
            raw_owner = ""
            if isinstance(task.metadata, dict):
                raw_mode = str(task.metadata.get("core_mode") or "").strip()
                raw_owner = str(task.metadata.get("memory_owner") or "").strip()

            mem_source = ""
            mem_user = ""
            if raw_owner and ":" in raw_owner:
                mem_source, mem_user = raw_owner.split(":", 1)
            elif raw_owner:
                mem_source, mem_user = raw_owner, "default"

            mode = (raw_mode or "").lower()
            # 兼容老配置：cron / heartbeat 都视为 background
            if mode in ("cron", "heartbeat"):
                mode = "background"

            frontend_id = mem_source or task.source
            dialog_id = mem_user or task.user_id

            if mode == "full":
                # full 模式下对齐 cli/feishu 主对话的权限策略：
                # - 是否允许 run_command 由 config.command_tools.allow_run 决定
                # - 其余参数复用 agent 配置
                cfg = get_config()
                profile = CoreProfile.full_from_config(
                    cfg,
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                )
            elif mode == "sub":
                profile = CoreProfile.default_sub(
                    allowed_tools=None,
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                )
            else:
                # 默认后台任务权限（定时任务 / 心跳）
                profile = CoreProfile.default_background(
                    frontend_id=frontend_id,
                    dialog_window_id=dialog_id,
                )

            # 有 memory_owner 时，为该 Core 打开持久化记忆；否则仅使用工作记忆。
            # 注意：full_from_config 默认 memory_enabled=True，因此必须显式设置为 False。
            if raw_owner:
                profile.memory_enabled = True
            else:
                profile.memory_enabled = False
            request = KernelRequest.create(
                text=task.instruction,
                session_id=task.session_id,
                frontend_id=task.source,
                metadata={
                    # source/user_id 仍表示“逻辑触发方”（cron + user_id），
                    # 实际记忆 owner 由 CoreProfile.frontend_id/dialog_window_id 决定。
                    "source": "cron",
                    "user_id": task.user_id,
                    "_hooks": hooks,
                },
                profile=profile,
            )
            future = await scheduler.submit(request)
            run_result = await future
            result = run_result.output_text
            op_ok, op_problems = task_logger.evaluate_required_operations()
            if op_ok:
                queue.update_status(task.task_id, TaskStatus.SUCCESS, result=result)
                activity_record = task_logger.log_task_end(
                    status=TaskStatus.SUCCESS, result=result, error=None
                )
            else:
                error_msg = "; ".join(op_problems)
                queue.update_status(
                    task.task_id, TaskStatus.FAILED, result=result, error=error_msg
                )
                activity_record = task_logger.log_task_end(
                    status=TaskStatus.FAILED, result=result, error=error_msg
                )
        except Exception as exc:
            logger.exception("Task %s failed: %s", task.task_id, exc)
            activity_record = task_logger.log_task_end(
                status=TaskStatus.FAILED, result=None, error=str(exc)
            )
            queue.update_status(task.task_id, TaskStatus.FAILED, error=str(exc))
        finally:
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

    client = FeishuClient(timeout_seconds=feishu_cfg.timeout_seconds)
    await client.send_text_message(chat_id=chat_id, text=text_out)


async def _main() -> None:
    cfg = get_config()
    # 工具在 daemon 进程内加载；修改工具实现/定义（如 file_tools.read_file）后需重启本 daemon 才能生效
    tools = get_default_tools(config=cfg)
    owner_id = (sys.argv[1].strip() if len(sys.argv) > 1 else "root") or "root"
    source = (sys.argv[2].strip() if len(sys.argv) > 2 else "cli") or "cli"
    default_session_id = f"{source}:default"

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
        tools_factory=lambda: get_default_tools(config=cfg),
        kernel=kernel,
        summarizer=summarizer,
        session_logger=None,
    )
    scheduler_runtime = KernelScheduler(kernel=kernel, core_pool=core_pool)
    stop_event = asyncio.Event()
    consumer_task = asyncio.create_task(
        _consume_loop(queue, scheduler_runtime, stop_event),
        name="automation-consumer",
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
            adapter = CoreSessionAdapter(created_agent)
            # 不在 factory 里调用 activate_session，由 gateway._create_session 根据
            # is_expired 状态决定 replay_messages_limit，避免全量历史被错误加载。
            return adapter

        gateway = AutomationCoreGateway(
            core_adapter,
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
        gateway.attach_scheduler(scheduler_runtime)
        await gateway.activate_primary_session()

        ipc = AutomationIPCServer(
            gateway,
            owner_id=owner_id,
            source=source,
            socket_path=default_socket_path(),
            policy=IPCServerPolicy(expire_check_interval_seconds=60),
        )

        await scheduler_runtime.start()
        await scheduler.start()
        await ipc.start()
        logger.info("Automation daemon started. socket=%s", ipc.socket_path)

        async def _connect_mcp_in_background() -> None:
            try:
                if await core_agent.ensure_mcp_connected():
                    logger.info("MCP connected (deferred)")
            except Exception as exc:
                # 单行警告，不刷屏；若需排查可开启 DEBUG 或查看 logs/automation_daemon.log
                logger.warning(
                    "MCP deferred connect failed: %s (%s). Daemon works without MCP tools.",
                    type(exc).__name__,
                    exc,
                )
                logger.debug("MCP deferred connect traceback", exc_info=True)

        mcp_task = asyncio.create_task(
            _connect_mcp_in_background(), name="daemon-mcp-connect"
        )

        def _mcp_done_cb(task: asyncio.Task[None]) -> None:
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return
            if exc is None:
                return
            # 抑制 anyio/mcp 在异步生成器关闭时的已知噪音，避免 "Task exception was never retrieved"
            msg = str(exc)
            if "Attempted to exit cancel scope in a different task" in msg or (
                isinstance(exc, RuntimeError) and "cancel scope" in msg
            ):
                logger.debug("MCP background connect teardown (ignored): %s", exc)
            # 其他异常已在 _connect_mcp_in_background 的 except 中打过，此处不再重复

        mcp_task.add_done_callback(_mcp_done_cb)

        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        finally:
            await ipc.stop()
            await scheduler.stop()
            await scheduler_runtime.stop()
            await core_pool.evict_all()
            await gateway.close()
    consumer_task.cancel()
    await asyncio.gather(consumer_task, return_exceptions=True)

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
