"""Logging helpers for queue-driven automation.

This module is responsible for:
- 人类可读的详细日志: logs/automation/tasks/<task_id>.jsonl
- Agent 可见的活动简报: data/automation/automation_activity.jsonl
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .agent_task import AgentTask, TaskStatus
from .repositories import _automation_base_dir


def _logs_base_dir() -> Path:
    """Base directory for human-readable automation logs."""
    base = Path("logs") / "automation" / "tasks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _activity_file_path() -> Path:
    """File path for agent-visible automation activity summary."""
    base_dir = _automation_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "automation_activity.jsonl"


@dataclass
class AutomationTaskLogger:
    """Per-task logger that records detailed trace and builds a compact activity record."""

    task: AgentTask
    log_path: Path = field(init=False)
    activity_path: Path = field(init=False)
    used_tools: Set[str] = field(default_factory=set, init=False)
    operation_details: List[Dict[str, Any]] = field(default_factory=list, init=False)
    _tool_alias_by_call_id: Dict[str, str] = field(default_factory=dict, init=False)
    started_at: datetime = field(default_factory=datetime.now, init=False)
    finished_at: Optional[datetime] = field(default=None, init=False)

    def __post_init__(self) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = _logs_base_dir() / f"automation-{ts}-{self.task.task_id}.jsonl"
        self.activity_path = _activity_file_path()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _append_json_line(self, payload: Dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload.setdefault("timestamp", datetime.now().isoformat())
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------
    # Public API used by 队列消费者（如 automation_daemon 内部的 SessionManager）
    # ------------------------------------------------------------------

    def log_task_start(self) -> None:
        self._append_json_line(
            {
                "type": "task_start",
                "task_id": self.task.task_id,
                "source": self.task.source,
                "session_id": self.task.session_id,
                "instruction": self.task.instruction,
                "context_policy": self.task.context_policy.value,
            }
        )

    def log_trace_event(self, event: Dict[str, Any]) -> None:
        """Record LLM/tool trace events from ScheduleAgent.on_trace_event."""
        etype = event.get("type")
        if etype == "tool_call":
            name = str(event.get("name") or "")
            tool_call_id = str(event.get("tool_call_id") or "")
            if name:
                self.used_tools.add(name)
                # 兼容 call_tool 间接调用：记录别名映射（tool_call_id -> inner tool name）
                if name == "call_tool":
                    arguments = event.get("arguments")
                    if isinstance(arguments, dict):
                        inner_name = arguments.get("name")
                        if isinstance(inner_name, str) and inner_name.strip() and tool_call_id:
                            self._tool_alias_by_call_id[tool_call_id] = inner_name
                self.operation_details.append(
                    {
                        "operation": name,
                        "stage": "call",
                        "tool_call_id": tool_call_id,
                        "arguments": event.get("arguments"),
                        "iteration": event.get("iteration"),
                    }
                )
        elif etype == "tool_result":
            name = str(event.get("name") or "")
            tool_call_id = str(event.get("tool_call_id") or "")
            operation_name = self._tool_alias_by_call_id.get(tool_call_id, name)
            if name:
                self.operation_details.append(
                    {
                        "operation": operation_name,
                        "reported_operation": name,
                        "stage": "result",
                        "tool_call_id": tool_call_id,
                        "success": bool(event.get("success", False)),
                        "message": event.get("message"),
                        "error": event.get("error"),
                        "duration_ms": event.get("duration_ms"),
                        "iteration": event.get("iteration"),
                    }
                )
        self._append_json_line({"type": "trace", "payload": event})

    def evaluate_required_operations(self) -> Tuple[bool, List[str]]:
        """
        Validate that critical operations for each cron source were executed successfully.

        Returns:
            (ok, problems)
        """
        required_ops_map: Dict[str, List[str]] = {
            "cron:sync.course": ["sync_canvas"],
            "cron:sync.email": ["sync_sources"],
            "cron:summary.daily": ["get_digest"],
            "cron:summary.weekly": ["get_digest"],
        }
        required_ops = required_ops_map.get(self.task.source, [])
        if not required_ops:
            return True, []

        success_ops = {
            detail["operation"]
            for detail in self.operation_details
            if detail.get("stage") == "result"
            and detail.get("success") is True
            and isinstance(detail.get("operation"), str)
        }
        problems: List[str] = []
        for op in required_ops:
            if op not in success_ops:
                problems.append(f"required operation failed or missing: {op}")
        return len(problems) == 0, problems

    def build_agent_activity_summary(self) -> Dict[str, Any]:
        """Build compact (operation + result) records for Agent-visible history."""
        operation_results: List[Dict[str, Any]] = []
        for detail in self.operation_details:
            if detail.get("stage") != "result":
                continue
            operation_results.append(
                {
                    "operation": detail.get("operation"),
                    "success": detail.get("success"),
                    "message": detail.get("message"),
                    "error": detail.get("error"),
                }
            )
        return {
            "operations": operation_results,
            "operation_count": len(operation_results),
        }

    def log_task_end(self, status: TaskStatus, result: Optional[str], error: Optional[str]) -> Dict[str, Any]:
        self.finished_at = datetime.now()
        self._append_json_line(
            {
                "type": "task_end",
                "task_id": self.task.task_id,
                "status": status.value,
                "result": (result or "")[:500],
                "error": error,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(),
            }
        )
        return self._append_activity_record(status=status, result=result, error=error)

    # ------------------------------------------------------------------
    # Agent-visible activity summary
    # ------------------------------------------------------------------

    def _append_activity_record(
        self,
        *,
        status: TaskStatus,
        result: Optional[str],
        error: Optional[str],
    ) -> Dict[str, Any]:
        """Write a compact (operation + result) summary to automation_activity.jsonl and return the record."""
        activity_summary = self.build_agent_activity_summary()
        record: Dict[str, Any] = {
            "timestamp": (self.finished_at or datetime.now()).isoformat(),
            "task_id": self.task.task_id,
            "source": self.task.source,
            "session_id": self.task.session_id,
            "instruction": self.task.instruction,
            "status": status.value,
            "operations": activity_summary["operations"],
            "operation_count": activity_summary["operation_count"],
            "result": {
                "success": status == TaskStatus.SUCCESS,
                "message": result if result else None,
                "error": error,
            },
            "error": error,
        }
        path = self.activity_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

