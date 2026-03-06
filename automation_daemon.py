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

from agent.automation import (
    AgentTaskQueue,
    AutomationCoreGateway,
    AutomationIPCServer,
    AutomationScheduler,
    IPCServerPolicy,
    SessionCutPolicy,
    SessionManager,
    SessionRegistry,
    default_socket_path,
)
from agent.automation.config_sync import sync_job_definitions_from_config
from agent.automation.agent_task import TaskStatus
from agent.automation.logging_utils import AutomationTaskLogger
from agent.automation.repositories import JobDefinitionRepository, JobRunRepository
from agent.config import get_config
from agent.core import ScheduleAgent, ScheduleAgentAdapter
from agent.utils.session_logger import SessionLogger

from agent.frontend.feishu.client import FeishuClient

from main import get_default_tools

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
    session_manager: SessionManager,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        task = queue.pop_pending()
        if task is None:
            try:
                await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        task_logger = AutomationTaskLogger(task)
        task_logger.log_task_start()
        activity_record: dict[str, Any] | None = None
        try:
            async def on_trace_event(event: dict) -> None:
                task_logger.log_trace_event(event)

            result = await session_manager.run_task(
                session_id=task.session_id,
                instruction=task.instruction,
                context_policy=task.context_policy,
                on_trace_event=on_trace_event,
            )
            op_ok, op_problems = task_logger.evaluate_required_operations()
            if op_ok:
                queue.update_status(task.task_id, TaskStatus.SUCCESS, result=result)
                activity_record = task_logger.log_task_end(status=TaskStatus.SUCCESS, result=result, error=None)
            else:
                error_msg = "; ".join(op_problems)
                queue.update_status(task.task_id, TaskStatus.FAILED, result=result, error=error_msg)
                activity_record = task_logger.log_task_end(status=TaskStatus.FAILED, result=result, error=error_msg)
        except Exception as exc:
            logger.exception("Task %s failed: %s", task.task_id, exc)
            activity_record = task_logger.log_task_end(status=TaskStatus.FAILED, result=None, error=str(exc))
            queue.update_status(task.task_id, TaskStatus.FAILED, error=str(exc))
        finally:
            if activity_record is not None:
                try:
                    await _maybe_notify_feishu_activity(activity_record)
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning("Failed to send Feishu automation activity notification: %s", notify_exc)


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

    # 会话日志记录器（daemon 级别）
    session_logger: SessionLogger | None = None
    if cfg.logging.enable_session_log:
        session_logger = SessionLogger(
            log_dir=cfg.logging.session_log_dir,
            enable_detailed_log=cfg.logging.enable_detailed_log,
            max_system_prompt_log_len=cfg.logging.max_system_prompt_log_len,
        )
        session_logger.on_session_start()

    queue = AgentTaskQueue()
    recovered = queue.recover_stale_running()
    if recovered:
        logger.info("Recovered %d stale running tasks", recovered)

    job_def_repo = JobDefinitionRepository()
    job_run_repo = JobRunRepository()
    sync_job_definitions_from_config(config=cfg, job_def_repo=job_def_repo)
    scheduler = AutomationScheduler(job_def_repo=job_def_repo, job_run_repo=job_run_repo, task_queue=queue)

    session_manager = SessionManager(config=cfg, tools_factory=lambda: get_default_tools(config=cfg))
    stop_event = asyncio.Event()
    consumer_task = asyncio.create_task(_consume_loop(queue, session_manager, stop_event), name="automation-consumer")

    # IPC core session and gateway (interactive frontends)
    async with ScheduleAgent(
        config=cfg,
        tools=tools,
        max_iterations=cfg.agent.max_iterations,
        timezone=cfg.time.timezone,
        user_id=owner_id,
        source=source,
        session_logger=session_logger,
    ) as core_agent:
        core_adapter = ScheduleAgentAdapter(core_agent)

        async def _session_factory(session_key: str) -> ScheduleAgentAdapter:
            created_agent = ScheduleAgent(
                config=cfg,
                tools=tools,
                max_iterations=cfg.agent.max_iterations,
                timezone=cfg.time.timezone,
                user_id=owner_id,
                source=source,
                session_logger=session_logger,
            )
            await created_agent.__aenter__()
            adapter = ScheduleAgentAdapter(created_agent)
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
        await gateway.activate_primary_session()

        ipc = AutomationIPCServer(
            gateway,
            owner_id=owner_id,
            source=source,
            socket_path=default_socket_path(),
            policy=IPCServerPolicy(expire_check_interval_seconds=60),
        )

        await scheduler.start()
        await ipc.start()
        logger.info("Automation daemon started. socket=%s", ipc.socket_path)

        try:
            while True:
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            stop_event.set()
            raise
        finally:
            await ipc.stop()
            await scheduler.stop()
            await gateway.close()

    await session_manager.close_all()
    consumer_task.cancel()
    await asyncio.gather(consumer_task, return_exceptions=True)

    if session_logger is not None:
        # Daemon 维度无法准确统计 turn_count / total_usage，这里仅记录会话结束事件。
        session_logger.on_session_end(turn_count=0, total_usage=None)
        session_logger.close()


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event()

    def _loop_exception_handler(loop_obj: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # 屏蔽 anyio/mcp 在 shutdown_asyncgens 阶段偶发的已知噪音：
        # RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
        message = str(context.get("message") or "")
        exc = context.get("exception")
        if (
            "an error occurred during closing of asynchronous generator" in message
            and isinstance(exc, RuntimeError)
            and "Attempted to exit cancel scope in a different task" in str(exc)
        ):
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
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
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
