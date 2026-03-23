"""
CoreCheckpoint — 跨 kernel 重启的会话状态检查点。

设计原则：TTL 仅在 kernel 存活期间计算（"暂停语义"）。
- Checkpoint 存 last_active_at（本 turn 结束的 wall-clock 时间）和会话状态。
- Kernel 关闭时写入 .kernel_last_shutdown_at；下次启动时扫描所有 checkpoint：
    - expired=True  → 已被正常 evict，清理文件并跳过
    - expired=False → 用 elapsed = shutdown_at - last_active_at 计算剩余 TTL
      - elapsed >= session_ttl → 超时，标记 expired=True 并跳过
      - elapsed <  session_ttl → 恢复为活跃 Core，TTL 从剩余时间继续计时

存储路径：data/memory/{source}/{user_id}/checkpoint.json
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CoreCheckpoint:
    """会话状态快照。"""

    session_id: str
    owner_id: str
    source: str
    # WorkingMemory 状态
    running_summary: Optional[str]
    recent_messages: List[Dict[str, Any]]
    # TTL 相关
    last_active_at: float           # time.time()，本 turn 结束时的 wall-clock
    remaining_ttl_seconds: float    # 保存 checkpoint 时的 session TTL 配置值（用于扫描时快速判断）
    # 对话元数据
    turn_count: int
    last_history_id: int            # 用于 _sync_external_session_updates
    token_usage: Dict[str, int]
    compression_round: int = 0  # 自总结（上下文折叠）已完成次数
    saved_at: float = field(default_factory=time.time)
    # 生命周期标记：True = 已被 evict，下次 kernel 启动时清理
    expired: bool = False


class CoreCheckpointManager:
    """负责单个 owner 检查点的读写删操作。

    Usage::

        mgr = CoreCheckpointManager(checkpoint_path="data/memory/cli/root/checkpoint.json")
        mgr.write(checkpoint)
        ckpt = mgr.read()       # 返回 CoreCheckpoint 或 None
        mgr.mark_expired()      # evict 后调用：写 expired=True，供下次启动清理
        mgr.delete()            # 立即物理删除
    """

    def __init__(self, checkpoint_path: str) -> None:
        self._path = Path(checkpoint_path)

    def write(self, checkpoint: CoreCheckpoint) -> None:
        """将 checkpoint 序列化写入磁盘（原子替换）。"""
        checkpoint.saved_at = time.time()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning("CoreCheckpointManager: write failed (%s): %s", self._path, exc)

    def read(self) -> Optional[CoreCheckpoint]:
        """从磁盘读取并反序列化 checkpoint；文件损坏或不存在则返回 None。

        优先读取 expired 字段：若为 True，调用方可直接跳过，无需解析其他字段。
        """
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return CoreCheckpoint(
                session_id=str(data.get("session_id", "")),
                owner_id=str(data.get("owner_id", "")),
                source=str(data.get("source", "")),
                running_summary=data.get("running_summary"),
                recent_messages=list(data.get("recent_messages", [])),
                last_active_at=float(data.get("last_active_at", 0.0)),
                remaining_ttl_seconds=float(data.get("remaining_ttl_seconds", 0.0)),
                turn_count=int(data.get("turn_count", 0)),
                last_history_id=int(data.get("last_history_id", 0)),
                token_usage=dict(data.get("token_usage", {})),
                compression_round=int(data.get("compression_round", 0)),
                saved_at=float(data.get("saved_at", 0.0)),
                expired=bool(data.get("expired", False)),
            )
        except Exception as exc:
            logger.warning("CoreCheckpointManager: read failed (%s): %s", self._path, exc)
            return None

    def mark_expired(self) -> None:
        """将 checkpoint 标记为已过期（evict 后调用）。

        保留文件并写入 expired=True，供下次 kernel 启动时识别并清理，
        同时为调试提供可见的「该 session 已被正常结束」记录。
        """
        checkpoint = self.read()
        if checkpoint is None:
            return
        checkpoint.expired = True
        self.write(checkpoint)

    def delete(self) -> None:
        """立即物理删除检查点文件。"""
        try:
            if self._path.exists():
                self._path.unlink()
        except Exception as exc:
            logger.warning("CoreCheckpointManager: delete failed (%s): %s", self._path, exc)
