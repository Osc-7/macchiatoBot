"""
统一记忆检索语料库（向量 / 关键词检索）

所有需被 memory_search 找回的内容写入 {owner}/corpus/，写入后 best-effort embed（QMD）。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from .long_term import _QMD_COLLECTION_NAME, _write_to_qmd_collection

_VALID_CATEGORIES = (
    "docs",
    "meeting",
    "diary",
    "lessons",
    "notes",
    "code",
    "other",
    "recent_topic",
)


class MemoryCorpus:
    """统一可检索记忆语料库。"""

    VALID_CATEGORIES = _VALID_CATEGORIES

    def __init__(
        self,
        corpus_dir: str,
        qmd_enabled: bool = False,
        qmd_command: str = "qmd",
    ):
        self._dir = Path(corpus_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._qmd_enabled = qmd_enabled
        self._qmd_command = qmd_command

    @property
    def root(self) -> Path:
        return self._dir

    def store_text(
        self,
        content: str,
        filename: str,
        category: str = "notes",
    ) -> Path:
        """将文本写入语料库并触发 embed。"""
        cat = category if category in self.VALID_CATEGORIES else "other"
        cat_dir = self._dir / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\-.]", "_", filename)
        if not safe_name.endswith(".md"):
            safe_name += ".md"
        md_path = cat_dir / safe_name
        md_path.write_text(content, encoding="utf-8")
        self._embed()
        return md_path

    def store_file(
        self,
        source_path: str,
        category: str = "docs",
        title: Optional[str] = None,
    ) -> Optional[Path]:
        """将文件转为 Markdown 写入语料库并触发 embed。"""
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

        self._embed()
        return md_path

    def store_entry_markdown(self, entry_id: str, markdown: str, category: str) -> Path:
        """写入单条 entry 镜像（如 recent_topic）。"""
        cat = category if category in self.VALID_CATEGORIES else "other"
        cat_dir = self._dir / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        md_path = cat_dir / f"{entry_id}.md"
        md_path.write_text(markdown, encoding="utf-8")
        self._embed()
        return md_path

    def _legacy_search_roots(self) -> List[Path]:
        """Pre-0.2.6 stores: content/ notes and long_term/markdown/ distilled entries."""
        owner = self._dir.parent
        roots: List[Path] = []
        for rel in ("content", "long_term/markdown"):
            p = owner / rel
            if p.is_dir():
                roots.append(p.resolve())
        return roots

    def search(self, query: str, top_n: int = 5) -> List[dict]:
        """
        检索语料库。QMD 开启时优先语义检索，不足则关键词补充；
        仍不足时回退扫描 legacy content/ 与 long_term/markdown/。

        Returns:
            [{"path": str, "snippet": str, "source": "corpus"|"legacy"}, ...]
        """
        results: List[dict] = []
        seen_paths: set[str] = set()

        if self._qmd_enabled:
            for hit in self._search_qmd(query, top_n):
                path = str(hit.get("path", hit.get("file", "")))
                if not path or not self._path_in_search_roots(path):
                    continue
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                snippet = str(hit.get("snippet", hit.get("content", "")))[:300]
                source = "corpus" if self._path_in_corpus(path) else "legacy"
                results.append({"path": path, "snippet": snippet, "source": source})
                if len(results) >= top_n:
                    return results

        if len(results) < top_n:
            for path, snippet, source in self._search_keyword(
                query, top_n, roots=[self._dir]
            ):
                sp = str(path)
                if sp in seen_paths:
                    continue
                seen_paths.add(sp)
                results.append(
                    {"path": sp, "snippet": snippet[:300], "source": source}
                )
                if len(results) >= top_n:
                    return results

        if len(results) < top_n:
            legacy_roots = self._legacy_search_roots()
            if legacy_roots:
                for path, snippet, source in self._search_keyword(
                    query, top_n, roots=legacy_roots
                ):
                    sp = str(path)
                    if sp in seen_paths:
                        continue
                    seen_paths.add(sp)
                    results.append(
                        {"path": sp, "snippet": snippet[:300], "source": source}
                    )
                    if len(results) >= top_n:
                        break
        return results

    def _path_in_corpus(self, path_str: str) -> bool:
        try:
            p = Path(path_str).resolve()
            root = self._dir.resolve()
            return root in p.parents or p == root
        except (OSError, ValueError):
            return path_str.startswith(str(self._dir))

    def _path_in_search_roots(self, path_str: str) -> bool:
        if self._path_in_corpus(path_str):
            return True
        try:
            p = Path(path_str).resolve()
        except (OSError, ValueError):
            return False
        for root in self._legacy_search_roots():
            if root in p.parents or p == root:
                return True
        return False

    def _search_keyword(
        self,
        query: str,
        top_n: int,
        *,
        roots: List[Path],
    ) -> List[Tuple[Path, str, str]]:
        query_lower = query.lower()
        scored: List[Tuple[float, Path, str, str]] = []
        corpus_root = self._dir.resolve()

        for root in roots:
            if not root.is_dir():
                continue
            source = "corpus" if root.resolve() == corpus_root else "legacy"
            for md_file in root.rglob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                score = sum(1 for w in query_lower.split() if w in text.lower())
                if score > 0:
                    snippet = self._extract_snippet(text, query_lower)
                    scored.append((score, md_file, snippet, source))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(path, snippet, source) for _, path, snippet, source in scored[:top_n]]

    def _search_qmd(self, query: str, top_n: int) -> List[dict]:
        try:
            result = subprocess.run(
                [self._qmd_command, "query", query, "--json", "-n", str(top_n * 2)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            raw = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("results", raw.get("hits", []))
        if not isinstance(raw, list):
            return []
        return raw

    def _embed(self) -> None:
        if self._qmd_enabled:
            _write_to_qmd_collection(self._dir, self._qmd_command, _QMD_COLLECTION_NAME)

    @staticmethod
    def _convert_with_markitdown(file_path: str) -> Optional[str]:
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
