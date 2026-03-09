"""
水源社区 Discourse API 客户端。

使用 User-Api-Key 访问水源社区（https://shuiyuan.sjtu.edu.cn）。
参考 ShuiyuanAutoReply：请求间保持最小间隔，降低 429 限流风险。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

SITE_URL_BASE = "https://shuiyuan.sjtu.edu.cn"

# 类级请求节流：所有 ShuiyuanClient 实例共享，避免多路复用造成 429
_request_lock: threading.Lock = threading.Lock()
_last_request_ts: float = 0.0
_request_interval: float = 2.0  # 秒，略低速以减少 429 概率


def _ensure_rate_limit() -> None:
    """请求前等待，保证与上次请求间隔至少 _request_interval 秒。"""
    global _last_request_ts
    with _request_lock:
        elapsed = time.monotonic() - _last_request_ts
        if elapsed < _request_interval:
            time.sleep(_request_interval - elapsed)
        _last_request_ts = time.monotonic()


def _format_rate_limit_headers(headers: Any) -> str:
    """从响应头中提取速率限制信息，便于在日志中展示。"""
    try:
        h = headers or {}
    except Exception:
        h = {}
    limit = h.get("X-RateLimit-Limit") or h.get("x-ratelimit-limit")
    remaining = h.get("X-RateLimit-Remaining") or h.get("x-ratelimit-remaining")
    reset = h.get("X-RateLimit-Reset") or h.get("x-ratelimit-reset")
    retry_after = h.get("Retry-After") or h.get("retry-after")
    parts = []
    if limit is not None:
        parts.append(f"limit={limit}")
    if remaining is not None:
        parts.append(f"remaining={remaining}")
    if reset is not None:
        parts.append(f"reset={reset}")
    if retry_after is not None:
        parts.append(f"retry_after={retry_after}")
    return ", ".join(parts)


class ShuiyuanRateLimitError(RuntimeError):
    """水源 API 限流异常，包含路径和头信息摘要，便于在 connector 中详细记录。"""

    def __init__(
        self,
        path: str,
        *,
        status_code: int = 429,
        headers: Any = None,
        body_preview: str = "",
    ) -> None:
        header_str = _format_rate_limit_headers(headers)
        msg = f"水源 API 限流({status_code}) path={path}"
        if header_str:
            msg += f" [{header_str}]"
        if body_preview:
            msg += f" body={body_preview}"
        super().__init__(msg)
        self.path = path
        self.status_code = status_code
        self.headers = headers
        self.body_preview = body_preview


def _raise_rate_limit(path: str, response: requests.Response) -> None:
    """将 429 响应转换为带详细信息的 ShuiyuanRateLimitError。"""
    try:
        body = (response.text or "")[:300].replace("\n", " ").strip()
    except Exception:
        body = ""
    raise ShuiyuanRateLimitError(
        path,
        status_code=response.status_code,
        headers=response.headers,
        body_preview=body,
    )


class ShuiyuanClientPool:
    """
    支持多 User-Api-Key 轮询与日级限流切换的客户端包装器。

    - 接口尽量与 ShuiyuanClient 保持一致，通过 __getattr__ 动态代理方法调用
    - 当某个 Key 触发日级限流（user_api_key_limiter_1_day）时：
      - 将该 Key 标记为在未来 cooldown_hours 小时内不可用（写入 state_path，进程重启仍生效）
      - 自动切换到下一把 Key 重试本次请求
    - 当所有 Key 均在冷却中时，抛出 RuntimeError 由上层处理
    """

    def __init__(
        self,
        user_api_keys: list[str],
        *,
        site_url: str = SITE_URL_BASE,
        timeout: float = 10.0,
        state_path: Optional[Path] = None,
        cooldown_hours: int = 5,
    ) -> None:
        if not user_api_keys:
            raise ValueError("user_api_keys 不能为空")
        self._keys: list[str] = [k.strip() for k in user_api_keys if k and k.strip()]
        if not self._keys:
            raise ValueError("user_api_keys 为空或仅包含空白字符串")

        self._site_url = site_url
        self._timeout = timeout
        self._state_path = state_path or Path("./data/shuiyuan/user_api_keys_state.json")
        self._cooldown_seconds = max(1, int(cooldown_hours * 3600))

        self._clients: dict[str, ShuiyuanClient] = {}
        self._blocked_until: dict[str, float] = {k: 0.0 for k in self._keys}
        self._current_index: int = 0

        self._load_state()

    # -------- 状态持久化 --------

    def _load_state(self) -> None:
        path = self._state_path
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                return
        except Exception:
            return

        blocked_until = data.get("blocked_until") or {}
        if isinstance(blocked_until, dict):
            for k in self._keys:
                v = blocked_until.get(k)
                if isinstance(v, (int, float)):
                    self._blocked_until[k] = float(v)

        idx = data.get("current_index")
        if isinstance(idx, int) and 0 <= idx < len(self._keys):
            self._current_index = idx

    def _save_state(self) -> None:
        path = self._state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "keys": self._keys,
                "blocked_until": self._blocked_until,
                "current_index": self._current_index,
            }
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # 状态持久化失败不应影响主流程
            pass

    # -------- Key 选择与限流处理 --------

    def _select_key(self) -> str:
        now = time.time()
        # 所有可用 key 的索引
        available_indices = [
            i for i, k in enumerate(self._keys) if self._blocked_until.get(k, 0.0) <= now
        ]
        if not available_indices:
            raise RuntimeError("所有 User-Api-Key 已在冷却中，请稍后再试")

        # 从上次位置开始轮询选择下一个可用 key
        n = len(self._keys)
        for offset in range(n):
            idx = (self._current_index + offset) % n
            key = self._keys[idx]
            if self._blocked_until.get(key, 0.0) <= now:
                self._current_index = idx
                self._save_state()
                return key

        # 理论上不会走到这里，因为 available_indices 非空
        raise RuntimeError("无法选择可用的 User-Api-Key")

    def _get_client_for_key(self, key: str) -> ShuiyuanClient:
        client = self._clients.get(key)
        if client is None:
            client = ShuiyuanClient(
                user_api_key=key,
                site_url=self._site_url,
                timeout=self._timeout,
            )
            self._clients[key] = client
        return client

    def _mark_rate_limited(self, key: str) -> None:
        now = time.time()
        self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), now + self._cooldown_seconds)
        self._save_state()

    def _should_mark_daily_limit(self, exc: Exception) -> bool:
        # 专门处理 ShuiyuanRateLimitError
        if isinstance(exc, ShuiyuanRateLimitError):
            headers = getattr(exc, "headers", None) or {}
            code = headers.get("discourse-rate-limit-error-code") or ""
            return str(code) == "user_api_key_limiter_1_day"

        # 其它 HTTPError，检查 response
        if isinstance(exc, requests.HTTPError):
            resp = getattr(exc, "response", None)
            if resp is not None and getattr(resp, "status_code", None) == 429:
                code = resp.headers.get("discourse-rate-limit-error-code") or ""
                return str(code) == "user_api_key_limiter_1_day"
        return False

    def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        last_error: Optional[Exception] = None
        tried: set[str] = set()

        while len(tried) < len(self._keys):
            key = self._select_key()
            if key in tried:
                break
            tried.add(key)
            client = self._get_client_for_key(key)

            try:
                method = getattr(client, method_name)
                return method(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if self._should_mark_daily_limit(e):
                    self._mark_rate_limited(key)
                    last_error = e
                    continue
                raise

        # 所有 key 都已尝试且遇到日级限流或其它错误
        if last_error is not None and self._should_mark_daily_limit(last_error):
            raise RuntimeError("所有 User-Api-Key 今日请求次数已达上限，请 5 小时后重试") from last_error
        if last_error is not None:
            raise last_error
        raise RuntimeError("未能使用任何 User-Api-Key 完成请求")

    def __getattr__(self, name: str) -> Any:
        # 动态代理未知属性为方法调用包装
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self._call(name, *args, **kwargs)

        return wrapper


class ShuiyuanClient:
    """
    水源社区 API 客户端。

    使用 User-Api-Key 认证，支持搜索、获取话题/帖子等只读操作。
    """

    def __init__(
        self,
        user_api_key: str,
        site_url: str = SITE_URL_BASE,
        timeout: float = 10.0,
    ):
        self._base = site_url.rstrip("/")
        self._headers = {"User-Api-Key": user_api_key}
        self._timeout = timeout

    def search(
        self,
        q: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """
        搜索水源社区。

        Args:
            q: 搜索关键词，支持 Discourse 语法如 tags:水源开发者
            page: 页码，默认 1

        Returns:
            Discourse search 接口返回的 JSON 字典
        """
        _ensure_rate_limit()
        r = requests.get(
            f"{self._base}/search.json",
            params={"q": q, "page": page},
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_topic(self, topic_id: int) -> Optional[dict[str, Any]]:
        """
        获取单个话题详情。

        Args:
            topic_id: 话题 ID

        Returns:
            话题 JSON 字典，不存在则 None
        """
        _ensure_rate_limit()
        r = requests.get(
            f"{self._base}/t/{topic_id}.json",
            headers=self._headers,
            timeout=self._timeout,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get_topic_posts(
        self,
        topic_id: int,
        post_ids: Optional[list[int]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        获取话题下的帖子列表。

        Args:
            topic_id: 话题 ID
            post_ids: 可选，指定要获取的帖子 ID 列表（最多 20 个）

        Returns:
            帖子列表 JSON 字典，不存在则 None
        """
        _ensure_rate_limit()
        url = f"{self._base}/t/{topic_id}/posts.json"
        if post_ids:
            if len(post_ids) > 20:
                post_ids = post_ids[:20]
            params = [("post_ids[]", pid) for pid in post_ids]
            r = requests.get(url, params=params, headers=self._headers, timeout=self._timeout)
        else:
            r = requests.get(url, headers=self._headers, timeout=self._timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get_topic_recent_posts(
        self,
        topic_id: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取话题下最近 N 条帖子（按楼层顺序）。

        Discourse 单次请求最多 20 条，会分批获取。
        """
        topic = self.get_topic(topic_id)
        if not topic:
            return []
        stream = topic.get("post_stream", {}).get("stream") or []
        if not stream:
            return []
        post_ids = stream[-limit:] if len(stream) > limit else stream
        all_posts: list[dict[str, Any]] = []
        batch_size = 20
        for i in range(0, len(post_ids), batch_size):
            batch = post_ids[i : i + batch_size]
            data = self.get_topic_posts(topic_id, post_ids=batch)
            if data:
                posts = data.get("post_stream", {}).get("posts") or []
                all_posts.extend(posts)
        return sorted(all_posts, key=lambda p: p.get("post_number", 0))

    def get_user_actions(
        self,
        username: str,
        *,
        filter_type: int = 7,
        offset: int = 0,
        limit: int = 60,
        acting_username: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        获取用户操作流（如 @ 提及）。

        参照 ShuiyuanAutoReply：使用 user_actions.json，filter=7 表示 mentions。
        acting_username：可选，限定「谁做的动作」。自 @ 时 acting_user=target_user，传 acting_username=owner 可显式获取自 @。
        返回按时间倒序（ newest first），便于 stream diff 只处理新项。
        """
        params: dict[str, Any] = {
            "username": username,
            "filter": filter_type,
            "offset": offset,
        }
        if acting_username:
            params["acting_username"] = acting_username
        _ensure_rate_limit()
        r = requests.get(
            f"{self._base}/user_actions.json",
            params=params,
            headers=self._headers,
            timeout=self._timeout,
        )
        if r.status_code == 429:
            _raise_rate_limit("/user_actions.json", r)
        r.raise_for_status()
        return r.json()

    def get_notifications(self, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """
        获取当前用户的通知（含 @ 提及等）。

        User-Api-Key 以当前用户身份请求，返回该用户收到的通知。
        notification_type: 1=mentioned, 2=replied, 3=quoted, ...
        """
        _ensure_rate_limit()
        r = requests.get(
            f"{self._base}/notifications.json",
            params={"limit": limit, "offset": offset},
            headers=self._headers,
            timeout=self._timeout,
        )
        if r.status_code == 429:
            _raise_rate_limit("/notifications.json", r)
        r.raise_for_status()
        return r.json()

    def get_post_by_id(
        self, topic_id: int, post_id: int
    ) -> Optional[dict[str, Any]]:
        """根据 post_id 直接获取单条帖子（含 raw 正文）。用于长帖，不依赖 stream 分页。"""
        data = self.get_topic_posts(topic_id, post_ids=[post_id])
        if not data:
            return None
        posts = data.get("post_stream", {}).get("posts") or []
        return posts[0] if posts else None

    def get_post_by_number(
        self, topic_id: int, post_number: int
    ) -> Optional[dict[str, Any]]:
        """根据楼层号获取单条帖子（依赖 topic stream，长帖可能失败，优先用 get_post_by_id）。"""
        topic = self.get_topic(topic_id)
        if not topic:
            return None
        stream = topic.get("post_stream", {}).get("stream") or []
        if post_number < 1 or post_number > len(stream):
            return None
        post_id = int(stream[post_number - 1])
        return self.get_post_by_id(topic_id, post_id)

    def toggle_retort(
        self,
        post_id: int,
        emoji: str,
        topic_id: Optional[int] = None,
    ) -> tuple[bool, int, str]:
        """
        对帖子贴表情（toggle，已有则取消）。

        水源使用 ShuiyuanSJTU/retort：PUT /retorts/:post_id；gdpelican/retort 使用 POST。
        emoji 为表情名，如 thumbsup、heart、+1，不要带冒号。

        Returns:
            (success, status_code, error_detail)
        """
        emoji_clean = (emoji or "").strip().strip(":").lower()
        if not emoji_clean:
            return False, 0, "emoji 不能为空"

        payload: dict[str, str] = {
            "retort": emoji_clean,
            # 某些部署可能使用 emoji 字段名，双写提升兼容性。
            "emoji": emoji_clean,
        }
        if topic_id is not None:
            payload["topic_id"] = str(topic_id)

        emoji_encoded = quote(emoji_clean, safe="")
        # 水源 ShuiyuanSJTU/retort 使用 PUT /retorts/:post_id（无 .json，见其 routes.rb）
        attempts: list[tuple[str, str, Optional[dict[str, str]]]] = [
            ("PUT", f"{self._base}/retorts/{post_id}", payload),  # 水源 retort 优先
            ("PUT", f"{self._base}/retorts/{post_id}.json", payload),
            ("POST", f"{self._base}/retorts/{post_id}.json", payload),
            ("POST", f"{self._base}/retorts/{post_id}", payload),
            (
                "POST",
                f"{self._base}/discourse-reactions/posts/{post_id}/custom-reactions/{emoji_encoded}/toggle.json",
                None,
            ),
            (
                "PUT",
                f"{self._base}/discourse-reactions/posts/{post_id}/custom-reactions/{emoji_encoded}/toggle.json",
                None,
            ),
        ]

        last_status = 0
        last_body = ""
        for method, url, data in attempts:
            _ensure_rate_limit()
            r = requests.request(
                method,
                url,
                data=data,
                headers=self._headers,
                timeout=self._timeout,
            )
            if r.status_code in (200, 201, 204):
                return True, r.status_code, ""
            try:
                body = (r.text or "")[:300].replace("\n", " ").strip()
            except Exception:
                body = ""
            last_status = r.status_code
            last_body = body
            # 接口不存在时继续尝试回退；其它错误（如 4xx 参数错误、5xx）直接返回。
            if r.status_code != 404:
                return False, r.status_code, body

        return False, last_status, last_body

    def create_post(
        self,
        raw: str,
        topic_id: int,
        reply_to_post_number: Optional[int] = None,
    ) -> tuple[Optional[dict[str, Any]], int, str]:
        """
        创建帖子或回复。需要 User-Api-Key 含 write scope。

        Returns:
            (post_dict, status_code, error_detail)
            - 成功: (post_dict, 200|201, "")
            - 失败: (None, status_code, error_detail)，error_detail 含响应体摘要
        """
        payload: dict[str, Any] = {"raw": raw, "topic_id": topic_id}
        if reply_to_post_number is not None:
            payload["reply_to_post_number"] = reply_to_post_number

        _ensure_rate_limit()
        r = requests.post(
            f"{self._base}/posts.json",
            json=payload,
            headers={**self._headers, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if r.status_code in (200, 201):
            return r.json(), r.status_code, ""

        try:
            body = (r.text or "")[:500].replace("\n", " ").strip()
        except Exception:
            body = ""

        return None, r.status_code, body
