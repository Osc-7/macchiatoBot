"""Canvas LMS API 客户端"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Union, Tuple
from urllib.parse import unquote
import httpx

from .config import CanvasConfig
from .models import (
    CanvasAssignment,
    CanvasEvent,
    CanvasPlannerItem,
    CanvasFile,
    now_utc,
)

logger = logging.getLogger(__name__)


def _filename_from_content_disposition(value: str) -> str:
    """从 Content-Disposition 头解析文件名（支持 filename*）。"""
    if not value:
        return ""
    m = re.search(r"filename\*=(?:UTF-8''|)([^;]+)", value, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m2 = re.search(r'filename="([^"]+)"', value)
    if m2:
        return m2.group(1)
    m3 = re.search(r"filename=([^;]+)", value)
    if m3:
        return m3.group(1).strip().strip('"')
    return ""


class CanvasAPIError(Exception):
    """Canvas API 错误基类"""

    pass


class CanvasAuthError(CanvasAPIError):
    """认证错误（401）"""

    pass


class CanvasRateLimitError(CanvasAPIError):
    """限流错误（429）"""

    pass


class CanvasClient:
    """Canvas LMS API 客户端

    提供与 Canvas LMS 交互的所有 API 方法。

    Attributes:
        config: Canvas 配置
        base_url: API 基础 URL
        headers: 请求头

    Example:
        >>> config = CanvasConfig.from_env()
        >>> client = CanvasClient(config)
        >>> profile = await client.get_user_profile()
        >>> print(f"Logged in as: {profile['name']}")
    """

    MAX_RETRIES = 3
    RETRY_DELAY = 1.0  # 秒
    TIMEOUT = 30.0  # 秒

    def __init__(self, config: CanvasConfig):
        """初始化 Canvas 客户端

        Args:
            config: Canvas 配置对象
        """
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "CanvasClient":
        """异步上下文管理器入口"""
        self._client = httpx.AsyncClient(
            headers=self.headers,
            timeout=self.TIMEOUT,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        if self._client:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        """获取 HTTP 客户端"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self.headers,
                timeout=self.TIMEOUT,
                follow_redirects=True,
            )
        return self._client

    async def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """发送 HTTP 请求

        处理分页、错误和重试。

        Args:
            method: HTTP 方法 (GET, POST, PUT, DELETE)
            endpoint: API 端点（不包含 /api/v1 前缀）
            params: URL 查询参数
            data: 请求体数据（用于 POST/PUT）

        Returns:
            API 响应数据（已解析为 JSON 或列表）

        Raises:
            CanvasAuthError: 认证失败（401）
            CanvasRateLimitError: 请求限流（429）
            CanvasAPIError: 其他 API 错误
        """
        url = f"{self.base_url}{endpoint}"
        client = self._get_client()

        last_error = None

        for attempt in range(self.MAX_RETRIES):
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=data,
                )

                # 处理常见错误
                if response.status_code == 401:
                    raise CanvasAuthError(
                        "Authentication failed. Please check your API key."
                    )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited. Waiting {retry_after} seconds...")
                    if attempt < self.MAX_RETRIES - 1:
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        # 重试耗尽，抛出限流异常
                        raise CanvasRateLimitError(
                            f"Rate limit exceeded after {self.MAX_RETRIES} attempts. "
                            f"Retry after {retry_after} seconds."
                        )

                # 5xx 错误也重试
                if response.status_code >= 500:
                    logger.warning(
                        f"Server error {response.status_code}, retrying... "
                        f"(attempt {attempt + 1}/{self.MAX_RETRIES})"
                    )
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self.RETRY_DELAY * (2**attempt)
                        await asyncio.sleep(delay)
                        continue

                if response.status_code >= 400:
                    raise CanvasAPIError(
                        f"API error {response.status_code}: {response.text[:200]}"
                    )

                # 检查是否有分页
                if "Link" in response.headers:
                    return await self._handle_pagination(
                        response, method, endpoint, params
                    )

                # 尝试解析 JSON
                try:
                    return response.json()
                except httpx.DecodingError:
                    return None

            except CanvasAuthError:
                # 认证错误不重试
                raise
            except CanvasRateLimitError:
                # 限流错误已处理
                raise
            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    f"Request failed (attempt {attempt + 1}/{self.MAX_RETRIES}): {e}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAY * (2**attempt)  # 指数退避
                    await asyncio.sleep(delay)
                else:
                    raise CanvasAPIError(
                        f"Request failed after {self.MAX_RETRIES} attempts: {e}"
                    ) from e

        # 不应该到这里，但为了类型检查
        if last_error:
            raise CanvasAPIError(
                f"Unexpected error after retries: {last_error}"
            ) from last_error
        raise CanvasAPIError("Unexpected error in request")

    async def _handle_pagination(
        self,
        response: httpx.Response,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]],
    ) -> List[Any]:
        """处理分页响应

        Canvas API 使用 Link header 进行分页，格式：
        <https://.../api/v1/courses?page=2>; rel="next"

        Args:
            response: 初始响应
            method: HTTP 方法
            endpoint: API 端点
            params: 查询参数

        Returns:
            所有页面的数据合并后的列表
        """
        results = response.json()
        if not isinstance(results, list):
            return results

        # 解析 Link header
        link_header = response.headers.get("Link", "")
        next_url = self._extract_next_url(link_header)

        while next_url:
            try:
                client = self._get_client()
                response = await client.get(next_url)

                if response.status_code >= 400:
                    logger.error(f"Failed to fetch next page: {response.status_code}")
                    break

                page_data = response.json()
                results.extend(page_data)

                link_header = response.headers.get("Link", "")
                next_url = self._extract_next_url(link_header)

            except Exception as e:
                logger.error(f"Error fetching next page: {e}")
                break

        return results

    def _extract_next_url(self, link_header: str) -> Optional[str]:
        """从 Link header 提取下一页 URL"""
        if not link_header:
            return None

        # 格式：<url>; rel="next"
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url_part = part.split(";")[0].strip()
                return url_part.strip("<>")

        return None

    # ========== API 方法 ==========

    async def get_user_profile(self) -> Dict[str, Any]:
        """获取当前用户信息

        Returns:
            用户信息字典，包含 id, name, login_id 等字段

        Example:
            >>> profile = await client.get_user_profile()
            >>> print(f"Hello, {profile['name']}!")
        """
        return await self.request("GET", "/users/self/profile")

    async def get_courses(
        self,
        enrollment_state: str = "active",
        include_term: bool = True,
    ) -> List[Dict[str, Any]]:
        """获取用户所有课程

        Args:
            enrollment_state: 注册状态 (active, completed, future)
            include_term: 是否包含学期信息

        Returns:
            课程列表

        Example:
            >>> courses = await client.get_courses()
            >>> for course in courses:
            ...     print(f"{course['name']} ({course['course_code']})")
        """
        params = {"enrollment_state[]": enrollment_state}
        if include_term:
            params["include[]"] = "term"

        return await self.request("GET", "/courses", params)

    async def get_assignments(
        self,
        course_id: int,
        include_submission: bool = True,
        all_dates: bool = True,
        course_name: Optional[str] = None,
    ) -> List[CanvasAssignment]:
        """获取课程作业列表

        Args:
            course_id: 课程 ID
            include_submission: 是否包含提交状态
            all_dates: 是否包含所有日期（due_at, lock_at, unlock_at）
            course_name: 课程名称（可选，避免重复查询）

        Returns:
            作业列表（CanvasAssignment 对象）

        Example:
            >>> assignments = await client.get_assignments(course_id=123)
            >>> for assignment in assignments:
            ...     print(f"{assignment.name} - Due: {assignment.due_at}")
        """
        params = {}
        if include_submission:
            params["include[]"] = "submission"
        if all_dates:
            params["all_dates"] = "true"

        # 如果未提供课程名称，且课程映射为空，则获取一次
        if not course_name:
            try:
                courses = await self.get_courses()
                course_map = {c["id"]: c["name"] for c in courses}
                course_name = course_map.get(course_id, "")
            except Exception as e:
                logger.warning(f"Failed to get course name for {course_id}: {e}")
                course_name = ""

        # 获取作业数据
        data = await self.request(
            "GET",
            f"/courses/{course_id}/assignments",
            params,
        )

        # 转换为模型对象
        assignments = []
        for item in data:
            try:
                assignment = CanvasAssignment.from_api_response(item, course_name)
                assignments.append(assignment)
            except Exception as e:
                logger.error(f"Failed to parse assignment {item.get('id')}: {e}")

        return assignments

    async def fetch_assignment_dict(
        self,
        course_id: int,
        assignment_id: int,
        include_submission: bool = True,
        all_dates: bool = True,
    ) -> Dict[str, Any]:
        """获取作业原始 JSON（含 attachments 等字段，若服务端返回）。

        对应 Canvas API: GET /courses/:course_id/assignments/:id
        """
        params: Dict[str, Any] = {}
        if include_submission:
            params["include[]"] = "submission"
        if all_dates:
            params["all_dates"] = "true"

        data = await self.request(
            "GET",
            f"/courses/{course_id}/assignments/{assignment_id}",
            params,
        )
        if not isinstance(data, dict):
            raise CanvasAPIError("Unexpected assignment response shape")
        return data

    async def get_assignment(
        self,
        course_id: int,
        assignment_id: int,
        include_submission: bool = True,
        all_dates: bool = True,
        course_name: Optional[str] = None,
    ) -> CanvasAssignment:
        """获取单门课程中的单个作业详情

        对应 Canvas API: GET /courses/:course_id/assignments/:id

        Args:
            course_id: 课程 ID
            assignment_id: 作业 ID
            include_submission: 是否包含当前用户的 submission（用于提交/评分状态）
            all_dates: 是否包含 due_at / lock_at / unlock_at 等全部日期字段
            course_name: 课程名称（可选，省略时会尝试从课程列表解析）

        Returns:
            CanvasAssignment 实例
        """
        data = await self.fetch_assignment_dict(
            course_id,
            assignment_id,
            include_submission=include_submission,
            all_dates=all_dates,
        )

        if not course_name:
            try:
                courses = await self.get_courses()
                course_map = {c["id"]: c["name"] for c in courses}
                course_name = course_map.get(course_id, "")
            except Exception as e:
                logger.warning(
                    f"Failed to get course name for assignment {assignment_id}: {e}"
                )
                course_name = ""

        return CanvasAssignment.from_api_response(data, course_name)

    async def get_file_metadata(
        self,
        file_id: int,
        course_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取文件元数据（含 size、display_name）。

        GET /files/:id 或 GET /courses/:course_id/files/:id
        """
        if course_id is not None:
            return await self.request(
                "GET",
                f"/courses/{course_id}/files/{file_id}",
            )
        return await self.request("GET", f"/files/{file_id}")

    async def download_file_bytes(
        self,
        file_id: int,
        *,
        course_id: Optional[int] = None,
        assignment_id: Optional[int] = None,
    ) -> Tuple[bytes, str]:
        """下载文件二进制内容。

        依次尝试（与 Canvas 文档一致）：
        - /assignments/:assignment_id/files/:id/download
        - /courses/:course_id/files/:id/download
        - /files/:id/download

        Returns:
            (content, filename_hint)  filename 来自 Content-Disposition 或文件元数据
        """
        if assignment_id is not None:
            path = f"/assignments/{assignment_id}/files/{file_id}/download"
        elif course_id is not None:
            path = f"/courses/{course_id}/files/{file_id}/download"
        else:
            path = f"/files/{file_id}/download"

        url = f"{self.base_url}{path}"
        client = self._get_client()
        response = await client.get(url, follow_redirects=True)

        if response.status_code == 401:
            raise CanvasAuthError(
                "Authentication failed. Please check your API key."
            )
        if response.status_code == 429:
            raise CanvasRateLimitError("Rate limited while downloading file.")
        if response.status_code >= 400:
            raise CanvasAPIError(
                f"File download error {response.status_code}: "
                f"{response.text[:200]}"
            )

        filename = _filename_from_content_disposition(
            response.headers.get("content-disposition", "")
        )
        if not filename:
            try:
                meta = await self.get_file_metadata(file_id, course_id=course_id)
                filename = (
                    meta.get("display_name")
                    or meta.get("filename")
                    or f"canvas_{file_id}"
                )
            except Exception as e:
                logger.warning(f"Could not resolve filename for file {file_id}: {e}")
                filename = f"canvas_{file_id}"

        return response.content, str(filename)

    async def get_calendar_events(
        self,
        start_date: str,
        end_date: str,
        event_types: Optional[List[str]] = None,
    ) -> List[CanvasEvent]:
        """获取日历事件

        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            event_types: 事件类型列表 (assignment, event)

        Returns:
            日历事件列表（CanvasEvent 对象）

        Example:
            >>> events = await client.get_calendar_events("2026-02-28", "2026-03-31")
            >>> for event in events:
            ...     print(f"{event.title} at {event.start_at}")
        """
        if event_types is None:
            event_types = ["assignment", "event"]

        params = {
            "start_date": start_date,
            "end_date": end_date,
            "event_types[]": event_types,
        }

        data = await self.request("GET", "/calendar_events", params)

        # 转换为模型对象
        events = []
        for item in data:
            try:
                event = CanvasEvent.from_api_response(item)
                events.append(event)
            except Exception as e:
                logger.error(f"Failed to parse event {item.get('id')}: {e}")

        return events

    async def get_submission(
        self,
        course_id: int,
        assignment_id: int,
    ) -> Dict[str, Any]:
        """获取作业提交详情

        Args:
            course_id: 课程 ID
            assignment_id: 作业 ID

        Returns:
            提交信息字典
        """
        return await self.request(
            "GET",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
        )

    async def get_course(
        self,
        course_id: int,
        include_syllabus: bool = True,
    ) -> Dict[str, Any]:
        """获取单门课程详情

        Args:
            course_id: 课程 ID
            include_syllabus: 是否包含 syllabus_body

        Returns:
            课程详情字典
        """
        params: Dict[str, Any] = {}
        if include_syllabus:
            params["include[]"] = "syllabus_body"
        return await self.request("GET", f"/courses/{course_id}", params)

    async def get_course_files(
        self,
        course_id: int,
        search_term: Optional[str] = None,
        content_types: Optional[List[str]] = None,
        sort: str = "name",
        order: str = "asc",
    ) -> List[CanvasFile]:
        """获取课程文件列表

        对应 Canvas API: GET /courses/:course_id/files

        Args:
            course_id: 课程 ID
            search_term: 搜索关键词（匹配文件名）
            content_types: 过滤的 MIME 列表（如 ['application/pdf']）
            sort: 排序字段（name, created_at, updated_at, size 等）
            order: 排序顺序（asc, desc）

        Returns:
            CanvasFile 列表
        """
        params: Dict[str, Any] = {
            "sort": sort,
            "order": order,
        }
        if search_term:
            params["search_term"] = search_term
        if content_types:
            params["content_types[]"] = content_types

        data = await self.request("GET", f"/courses/{course_id}/files", params)

        files: List[CanvasFile] = []
        for raw in data:
            try:
                files.append(CanvasFile.from_api_response(raw))
            except Exception as e:
                logger.error(f"Failed to parse file {raw.get('id')}: {e}")
        return files

    # ========== 便捷方法 ==========

    async def get_all_assignments(
        self,
        course_ids: Optional[List[int]] = None,
        concurrency: int = 5,
    ) -> List[CanvasAssignment]:
        """获取所有课程的作业

        Args:
            course_ids: 课程 ID 列表，如果为 None 则获取所有活跃课程
            concurrency: 并发请求数（默认 5，避免限流）

        Returns:
            所有作业的列表
        """
        # 只调用一次 get_courses()，同时获取课程 ID 列表和名称映射
        courses = await self.get_courses()
        course_map = {c["id"]: c["name"] for c in courses}

        if course_ids is None:
            course_ids = list(course_map.keys())

        all_assignments = []
        semaphore = asyncio.Semaphore(concurrency)

        async def fetch_course_assignments(course_id: int) -> List[CanvasAssignment]:
            """带信号量控制的作业获取"""
            async with semaphore:
                try:
                    course_name = course_map.get(course_id, "")
                    assignments = await self.get_assignments(
                        course_id,
                        course_name=course_name,
                    )
                    logger.info(
                        f"Got {len(assignments)} assignments from course {course_id}"
                    )
                    return assignments
                except Exception as e:
                    logger.error(
                        f"Failed to get assignments from course {course_id}: {e}"
                    )
                    return []

        # 并发获取所有课程的作业
        tasks = [fetch_course_assignments(cid) for cid in course_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                all_assignments.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Exception in gather: {result}")

        return all_assignments

    async def get_upcoming_events(
        self,
        days: int = 60,
        start_date: Optional[str] = None,
    ) -> List[CanvasEvent]:
        """获取未来 N 天的事件

        Args:
            days: 未来多少天
            start_date: 开始日期（默认今天）

        Returns:
            即将到来的事件列表
        """
        if start_date is None:
            start_date = datetime.now().strftime("%Y-%m-%d")

        end_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

        return await self.get_calendar_events(start_date, end_date)

    async def get_upcoming_assignments(
        self,
        days: int = 60,
        include_submitted: bool = False,
    ) -> List[CanvasAssignment]:
        """获取未来 N 天的作业

        Args:
            days: 未来多少天
            include_submitted: 是否包含已提交的作业

        Returns:
            即将到来的作业列表
        """
        all_assignments = await self.get_all_assignments()

        cutoff_date = now_utc() + timedelta(days=days)

        upcoming = []
        for assignment in all_assignments:
            # 跳过没有截止时间的
            if not assignment.due_at:
                continue

            # 确保时区一致
            due = assignment.due_at
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)

            # 跳过已过期的
            if due < now_utc():
                continue

            # 跳过超过 cutoff 的
            if due > cutoff_date:
                continue

            # 跳过已提交的（如果不需要）
            if not include_submitted and assignment.is_submitted:
                continue

            upcoming.append(assignment)

        # 按截止时间排序
        upcoming.sort(key=lambda a: a.due_at)

        return upcoming

    async def get_planner_items(
        self,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        filter: Optional[str] = None,
        context_codes: Optional[List[str]] = None,
    ) -> List[CanvasPlannerItem]:
        """获取 Canvas Planner 待办/机会项列表

        对应 Canvas Planner API: GET /planner/items

        Args:
            start_date: 起始日期（YYYY-MM-DD 或 datetime），为空则默认今天
            end_date: 结束日期（YYYY-MM-DD 或 datetime），为空则默认 start_date+30 天
            filter: 过滤条件: new_activity | incomplete_items | complete_items
            context_codes: 上下文过滤（如 ["course_42", "group_1"]），默认所有课程/小组

        Returns:
            CanvasPlannerItem 列表

        参考文档:
            https://canvas.instructure.com/doc/api/planner.html#get-planner-items
        """

        # 处理日期参数
        def _normalize_date(value: Optional[Union[str, datetime]]) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, datetime):
                # Canvas 支持 ISO8601 字符串；这里统一使用日期部分
                return value.strftime("%Y-%m-%d")
            return value

        if start_date is None:
            start_date_str = datetime.now().strftime("%Y-%m-%d")
        else:
            start_date_str = _normalize_date(start_date)

        if end_date is None:
            end_date_str = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        else:
            end_date_str = _normalize_date(end_date)

        params: Dict[str, Any] = {
            "start_date": start_date_str,
            "end_date": end_date_str,
        }
        if filter:
            params["filter"] = filter
        if context_codes:
            # 与其他数组参数保持一致（如 event_types[]）
            params["context_codes[]"] = context_codes

        data = await self.request("GET", "/planner/items", params)

        items: List[CanvasPlannerItem] = []
        for raw in data:
            try:
                item = CanvasPlannerItem.from_api_response(raw)
                # 某些异常数据可能没有 plannable_id，直接跳过以免干扰后续逻辑
                if item.plannable_id is not None:
                    items.append(item)
            except Exception as e:
                logger.error(f"Failed to parse planner item: {e}")

        return items

    async def test_connection(self) -> bool:
        """测试 API 连接

        Returns:
            bool: 连接是否成功
        """
        try:
            profile = await self.get_user_profile()
            logger.info(f"Successfully connected to Canvas as {profile['name']}")
            return True
        except CanvasAuthError as e:
            logger.error(f"Authentication failed: {e}")
            return False
        except CanvasAPIError as e:
            logger.error(f"API error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return False

    async def close(self) -> None:
        """关闭客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None
