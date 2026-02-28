"""
Canvas 同步工具。

将 Canvas 课程作业/日历事件同步为本地日程事件。
"""

import os
from typing import Optional

from canvas_integration import CanvasClient, CanvasConfig, CanvasSync
from schedule_agent.config import Config

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from .storage_tools import AddEventTool


class SyncCanvasTool(BaseTool):
    """同步 Canvas 数据到本地日程。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        add_event_tool: Optional[AddEventTool] = None,
    ):
        self._config = config
        self._add_event_tool = add_event_tool or AddEventTool()

    @property
    def name(self) -> str:
        return "sync_canvas"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="sync_canvas",
            description="""从 Canvas 拉取作业和课程事件，并同步到本地日程。

适用场景：
- 用户要求“同步 Canvas 作业/课程安排”
- 用户希望把近期截止作业自动加入日程

工具会自动：
- 调用 Canvas API 拉取未来 N 天数据
- 将作业/事件转换为本地 add_event 数据
- 返回同步统计（创建/跳过/错误）
""",
            parameters=[
                ToolParameter(
                    name="days_ahead",
                    type="integer",
                    description="可选，同步未来多少天的数据；默认使用配置值",
                    required=False,
                ),
                ToolParameter(
                    name="include_submitted",
                    type="boolean",
                    description="可选，是否包含已提交作业；默认使用配置值",
                    required=False,
                ),
            ],
            usage_notes=[
                "需要配置 Canvas API Key（config.canvas.api_key 或环境变量 CANVAS_API_KEY）",
                "该工具会实际写入本地日程存储（调用 add_event）",
            ],
            tags=["canvas", "同步", "日程"],
        )

    def _build_canvas_config(self) -> Optional[CanvasConfig]:
        cfg = self._config.canvas if self._config else None
        api_key = (
            (cfg.api_key if cfg and cfg.api_key else None)
            or os.getenv("CANVAS_API_KEY")
        )
        if not api_key:
            return None

        base_url = (cfg.base_url if cfg else None) or os.getenv(
            "CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"
        )
        return CanvasConfig(api_key=api_key, base_url=base_url)

    async def _create_schedule_event(self, event_data: dict) -> Optional[str]:
        result = await self._add_event_tool.execute(**event_data)
        if not result.success:
            return None
        metadata = result.metadata or {}
        event_id = metadata.get("event_id")
        return str(event_id) if event_id else None

    async def execute(self, **kwargs) -> ToolResult:
        canvas_config = self._build_canvas_config()
        if canvas_config is None:
            return ToolResult(
                success=False,
                error="CANVAS_API_KEY_MISSING",
                message="未配置 Canvas API Key，请设置 config.canvas.api_key 或环境变量 CANVAS_API_KEY",
            )

        if not canvas_config.validate():
            return ToolResult(
                success=False,
                error="CANVAS_CONFIG_INVALID",
                message="Canvas 配置无效，请检查 base_url 和 api_key",
            )

        default_days = (
            self._config.canvas.default_days_ahead if self._config else 60
        )
        default_include_submitted = (
            self._config.canvas.include_submitted if self._config else False
        )
        days_ahead = int(kwargs.get("days_ahead", default_days))
        include_submitted = bool(
            kwargs.get("include_submitted", default_include_submitted)
        )

        try:
            async with CanvasClient(canvas_config) as client:
                syncer = CanvasSync(
                    client=client,
                    event_creator=self._create_schedule_event,
                )
                sync_result = await syncer.sync_to_schedule(
                    days_ahead=days_ahead,
                    include_submitted=include_submitted,
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error="CANVAS_SYNC_FAILED",
                message=f"Canvas 同步失败: {e}",
            )

        return ToolResult(
            success=len(sync_result.errors) == 0,
            message=(
                f"Canvas 同步完成：创建 {sync_result.created_count}，"
                f"跳过 {sync_result.skipped_count}，错误 {len(sync_result.errors)}"
            ),
            data=sync_result.to_dict(),
            metadata={"source": "canvas"},
        )
