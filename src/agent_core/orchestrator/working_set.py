"""
工具工作集管理。

负责维护 pinned 工具 + LRU 工作集，并构建每轮可见 snapshot。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Set

from agent_core.tools.versioned_registry import VersionedToolRegistry


@dataclass(frozen=True)
class ToolSnapshot:
    """某一轮推理使用的工具快照。"""

    version: int
    tool_names: List[str]
    openai_tools: List[dict]


class ToolWorkingSetManager:
    """
    管理 LLM 可见工具集合。
    """

    def __init__(self, pinned_tools: List[str], working_set_size: int = 6):
        self._lock = threading.RLock()
        self._pinned_tools: Set[str] = set(pinned_tools)
        self._working_set_size = max(0, int(working_set_size))
        self._active_tools: "OrderedDict[str, None]" = OrderedDict()
        self._registry_version = -1

    @property
    def pinned_tools(self) -> Set[str]:
        with self._lock:
            return set(self._pinned_tools)

    def pin(self, name: str) -> None:
        """动态将工具加入 pinned 集合（幂等）。"""
        name = (name or "").strip()
        if not name:
            return
        with self._lock:
            if name in self._pinned_tools:
                return
            self._pinned_tools.add(name)
            # 从 LRU 中移除，避免重复
            self._active_tools.pop(name, None)

    def unpin(self, name: str) -> None:
        """动态将工具从 pinned 集合中移除（幂等）。"""
        name = (name or "").strip()
        if not name:
            return
        with self._lock:
            self._pinned_tools.discard(name)

    def add_to_working_set(self, tool_names: List[str]) -> None:
        """将工具加入 LRU 工作集。"""
        if not tool_names:
            return
        with self._lock:
            for name in tool_names:
                if not name or name in self._pinned_tools:
                    continue
                self._active_tools[name] = None
                self._active_tools.move_to_end(name)
            while len(self._active_tools) > self._working_set_size:
                self._active_tools.popitem(last=False)

    def build_snapshot(self, registry: VersionedToolRegistry) -> ToolSnapshot:
        """
        构建当前轮次工具快照（immutable）。
        """
        version, all_tools = registry.list_tools()
        with self._lock:
            if version != self._registry_version:
                # 工具集合变化后，清理已经不存在的 LRU 条目
                active_filtered: "OrderedDict[str, None]" = OrderedDict()
                for name in self._active_tools.keys():
                    if name in all_tools:
                        active_filtered[name] = None
                self._active_tools = active_filtered
                self._registry_version = version

            pinned_visible = [n for n in self._pinned_tools if n in all_tools]
            lru_recent = list(self._active_tools.keys())[-self._working_set_size :]
            visible_names = pinned_visible + [
                n for n in lru_recent if n not in self._pinned_tools
            ]

        openai_tools = registry.get_openai_tools(visible_names)
        return ToolSnapshot(
            version=version, tool_names=visible_names, openai_tools=openai_tools
        )

    def get_visible_tool_names(self) -> List[str]:
        """返回当前工作集可见名称（不含 registry 过滤）。"""
        with self._lock:
            return (
                list(self._pinned_tools)
                + list(self._active_tools.keys())[-self._working_set_size :]
            )
