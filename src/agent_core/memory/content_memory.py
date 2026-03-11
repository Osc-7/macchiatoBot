"""
内容记忆 - 外部资料的 Markdown 统一存储与检索

将 PDF、笔记、代码文档、会议记录等通过 markitdown 转换后存入内容记忆库，
支持关键词检索和 QMD 语义检索。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


class ContentMemory:
    """内容记忆管理器。"""

    VALID_CATEGORIES = ("docs", "meeting", "diary", "lessons", "notes", "code", "other")

    def __init__(
        self, content_dir: str, qmd_enabled: bool = False, qmd_command: str = "qmd"
    ):
        self._dir = Path(content_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._qmd_enabled = qmd_enabled
        self._qmd_command = qmd_command

    def ingest_file(
        self,
        source_path: str,
        category: str = "other",
        title: Optional[str] = None,
    ) -> Optional[Path]:
        """
        将文件转为 Markdown 并存入内容记忆库。

        Args:
            source_path: 源文件路径
            category: 分类（docs, meeting, diary, lessons, notes, code, other）
            title: 自定义标题，若为 None 则从文件名推导

        Returns:
            存储后的 Markdown 文件路径，失败返回 None
        """
        src = Path(source_path)
        if not src.exists():
            return None

        cat = category if category in self.VALID_CATEGORIES else "other"
        cat_dir = self._dir / cat
        cat_dir.mkdir(parents=True, exist_ok=True)

        stem = title or src.stem
        safe_name = re.sub(r"[^\w\-.]", "_", stem)
        md_path = cat_dir / f"{safe_name}.md"

        if src.suffix.lower() == ".md":
            md_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            converted = self._convert_with_markitdown(str(src))
            if converted is None:
                return None
            md_path.write_text(converted, encoding="utf-8")

        return md_path

    def ingest_text(
        self,
        content: str,
        filename: str,
        category: str = "other",
    ) -> Path:
        """直接将文本内容写入内容记忆库。"""
        cat = category if category in self.VALID_CATEGORIES else "other"
        cat_dir = self._dir / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\-.]", "_", filename)
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        md_path = cat_dir / safe_name
        md_path.write_text(content, encoding="utf-8")
        return md_path

    def list_files(self, category: Optional[str] = None) -> List[Path]:
        """列出内容记忆中的文件。"""
        if category:
            cat_dir = self._dir / category
            if not cat_dir.exists():
                return []
            return sorted(cat_dir.glob("*.md"))
        return sorted(self._dir.rglob("*.md"))

    def search(self, query: str, top_n: int = 5) -> List[Tuple[Path, str]]:
        """
        在内容记忆中搜索。

        Returns:
            匹配的 (文件路径, 匹配片段) 列表
        """
        query_lower = query.lower()
        results: List[Tuple[float, Path, str]] = []

        for md_file in self._dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            score = sum(1 for w in query_lower.split() if w in text.lower())
            if score > 0:
                snippet = self._extract_snippet(text, query_lower)
                results.append((score, md_file, snippet))

        results.sort(key=lambda x: x[0], reverse=True)
        return [(path, snippet) for _, path, snippet in results[:top_n]]

    def search_qmd(self, query: str, top_n: int = 5) -> List[dict]:
        """通过 QMD CLI 执行语义检索（需 qmd_enabled）。"""
        if not self._qmd_enabled:
            return []
        try:
            result = subprocess.run(
                [self._qmd_command, "query", query, "--json", "-n", str(top_n)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                import json

                return json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass
        return []

    @staticmethod
    def _convert_with_markitdown(file_path: str) -> Optional[str]:
        """调用 markitdown 将文件转为 Markdown。"""
        try:
            from markitdown import MarkItDown

            converter = MarkItDown()
            result = converter.convert(file_path)
            return result.text_content
        except ImportError:
            try:
                result = subprocess.run(
                    ["markitdown", file_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    return result.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_snippet(text: str, query_lower: str, max_len: int = 200) -> str:
        """从文本中提取包含查询关键词的片段。"""
        words = query_lower.split()
        best_pos = 0
        best_score = 0
        for i in range(0, len(text), 50):
            chunk = text[i : i + max_len].lower()
            score = sum(1 for w in words if w in chunk)
            if score > best_score:
                best_score = score
                best_pos = i
        return text[best_pos : best_pos + max_len].strip()
