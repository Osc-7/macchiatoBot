"""Canvas LMS API 客户端"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Union
import httpx

from .config import CanvasConfig
from .models import CanvasAssignment, CanvasEvent, now_utc

logger = logging.getLogger(__name__)


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
                    logger.warning(
                        f"Rate limited. Waiting {retry_after} seconds..."
                    )
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
                        delay = self.RETRY_DELAY * (2 ** attempt)
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
                    delay = self.RETRY_DELAY * (2 ** attempt)  # 指数退避
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
        
        end_date = (
            datetime.now() + timedelta(days=days)
        ).strftime("%Y-%m-%d")
        
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
