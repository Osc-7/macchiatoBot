"""
水源社区 Discourse API 客户端。

使用 User-Api-Key 访问水源社区（https://shuiyuan.sjtu.edu.cn）。
"""

from typing import Any, Optional

import requests

SITE_URL_BASE = "https://shuiyuan.sjtu.edu.cn"


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
