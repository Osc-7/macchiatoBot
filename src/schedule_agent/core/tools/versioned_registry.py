"""
带版本号的工具注册表。

用于在 Agent Kernel 模式下支持：
- copy-on-write 更新
- snapshot 读取
- 基础关键字搜索
- 标签搜索
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseTool, ToolResult


@dataclass
class ToolSearchItem:
    """工具搜索返回项。"""

    name: str
    description: str
    parameters: List[Dict[str, Any]]
    tags: List[str]
    score: float

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "tags": self.tags,
            "score": round(self.score, 4),
        }


class VersionedToolRegistry:
    """
    带版本号和快照语义的工具注册表。

    说明：
    - 写操作采用 copy-on-write，确保读路径始终拿到稳定快照
    - 通过 version 变化感知工具集合更新
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._version = 0
        self._tools: Dict[str, BaseTool] = {}

    def list_tools(self) -> Tuple[int, Dict[str, BaseTool]]:
        """
        获取当前版本和工具快照。

        Returns:
            (version, tools_copy)
        """
        with self._lock:
            return self._version, self._tools.copy()

    def register(self, tool: BaseTool) -> None:
        """
        注册工具。

        Raises:
            ValueError: 工具名称已存在
        """
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"工具 '{tool.name}' 已注册")
            next_tools = self._tools.copy()
            next_tools[tool.name] = tool
            self._tools = next_tools
            self._version += 1

    def update_tools(self, tools: List[BaseTool]) -> None:
        """
        批量更新工具（按 name 覆盖）。
        """
        with self._lock:
            next_tools = self._tools.copy()
            for tool in tools:
                next_tools[tool.name] = tool
            self._tools = next_tools
            self._version += 1

    def unregister(self, name: str) -> bool:
        """
        注销工具。
        """
        with self._lock:
            if name not in self._tools:
                return False
            next_tools = self._tools.copy()
            del next_tools[name]
            self._tools = next_tools
            self._version += 1
            return True

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def get(self, name: str) -> Optional[BaseTool]:
        with self._lock:
            return self._tools.get(name)

    def list_names(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())

    def get_openai_tools(self, names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        获取 OpenAI Function Calling 工具定义。
        """
        with self._lock:
            if names is None:
                tools = list(self._tools.values())
            else:
                tools = [self._tools[name] for name in names if name in self._tools]
        return [tool.to_openai_tool() for tool in tools]

    def get_all_definitions(self) -> List[Dict[str, Any]]:
        """
        兼容旧接口：返回全部工具定义。
        """
        return self.get_openai_tools()

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """
        执行工具。
        """
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                error="TOOL_NOT_FOUND",
                message=f"工具 '{tool_name}' 不存在",
            )
        return await tool.execute(**kwargs)

    def search(
        self,
        query: str,
        limit: int = 8,
        exclude_names: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        按关键字和/或标签搜索工具。
        
        支持：
        - 关键词搜索：匹配 name / description / 参数描述
        - 标签搜索：匹配工具标签
        - 组合搜索：同时使用关键词和标签
        
        Args:
            query: 搜索关键词（可为空）
            limit: 返回数量上限
            exclude_names: 要排除的工具名称列表
            tags: 要匹配的标签列表（可为空）
        
        Returns:
            搜索结果列表，按分数降序排列
        """
        with self._lock:
            tools = self._tools.copy()

        exclude = set(exclude_names or [])
        q = (query or "").strip().lower()
        tokens = [t for t in re.split(r"[\s,，。:：;；/\\|]+", q) if t]
        tag_filter = set(tags) if tags else set()
        items: List[ToolSearchItem] = []

        for name, tool in tools.items():
            if name in exclude:
                continue
            definition = tool.get_definition()
            
            # 标签过滤
            if tag_filter and not tag_filter.intersection(definition.tags):
                continue
            
            text_parts: List[str] = [name, definition.description]
            params_meta: List[Dict[str, Any]] = []
            for param in definition.parameters:
                text_parts.extend([param.name, param.description])
                params_meta.append(
                    {
                        "name": param.name,
                        "type": param.type,
                        "required": param.required,
                        "description": param.description,
                    }
                )

            corpus = " ".join(text_parts).lower()
            score = 0.0
            
            # 关键词评分
            if q:
                if q in corpus:
                    score += 2.0
                score += sum(1.0 for token in tokens if token in corpus)
            else:
                score = 1.0
            
            # 标签匹配加分
            if tag_filter:
                matched_tags = tag_filter.intersection(definition.tags)
                score += len(matched_tags) * 0.5

            if score <= 0:
                continue

            items.append(
                ToolSearchItem(
                    name=name,
                    description=definition.description,
                    parameters=params_meta,
                    tags=definition.tags,
                    score=score,
                )
            )

        items.sort(key=lambda x: (-x.score, x.name))
        if limit > 0:
            items = items[:limit]
        return [item.to_dict() for item in items]

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
