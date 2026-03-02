"""上海交通大学教学信息服务网课表同步工具。

本模块提供一个只读工具，用于从教学信息服务网获取本科生课表 (kbList)，
并以结构化形式返回给 Agent。

支持两种获取登录态的方式：
- 自动方式：在工具参数中设置 refresh_cookies=true，使用 Playwright 打开浏览器完成 jAccount 登录，
  并自动将 Cookie 写入 config.sjtu_jw.cookies_path 指定的 JSON 文件。
- 手动方式：用户自行在浏览器中登录并导出 Cookie 为 JSON 文件，保存到 cookies_path。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from schedule_agent.config import SjtuJwConfig

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


SJTU_UNDERGRAD_COURSE_URL = "https://i.sjtu.edu.cn/kbcx/xskbcx_cxXsKb.html"
SJTU_LOGIN_URL = "https://i.sjtu.edu.cn/jaccountlogin"
SJTU_AFTER_LOGIN_PATTERN = "index_initMenu.html"

try:  # 可选依赖，运行时按需检测
    from playwright.async_api import async_playwright  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - 测试环境通常不安装 Playwright
    async_playwright = None  # type: ignore[assignment]


def _get_async_playwright():
    """延迟导入 Playwright，避免进程内安装后需重启才能识别。"""
    global async_playwright
    if async_playwright is None:
        try:
            from playwright.async_api import async_playwright as ap  # type: ignore[import-untyped]
            async_playwright = ap
        except Exception:
            pass
    return async_playwright


class SjtuJwClientError(Exception):
    """教学信息服务网客户端错误。"""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.detail = detail


def guess_academic_year_and_term(today: Optional[date] = None) -> Tuple[str, str]:
    """根据当前日期粗略推断学年和学期。

    返回:
        year: 学年，使用学年开始年份（如 2025 表示 2025-2026 学年）
        term: 学期，"1"=第一学期，"2"=第二学期
    """
    if today is None:
        today = date.today()

    year = today.year
    month = today.month

    # 规则（近似）：
    # - 9-12 月：当学年的第一学期，学年=当年
    # - 1 月：上一年学年的第一学期，学年=上一年
    # - 2-8 月：上一年学年的第二学期，学年=上一年
    if month >= 9:
        return str(year), "1"
    if month == 1:
        return str(year - 1), "1"
    # 2-8 月
    return str(year - 1), "2"


def _convert_term_to_xqm(term: str) -> str:
    """将学期编号转换为教学信息服务网的 xqm 参数值。

    参考 CourseBlock 实现:
        1 -> "3"
        2 -> "12"
        3 -> "16"
    """
    t = term.strip()
    if t in {"1", "一", "上"}:
        return "3"
    if t in {"2", "二", "下"}:
        return "12"
    if t in {"3", "三", "短学期"}:
        return "16"
    # 默认按第一学期处理
    return "3"


@dataclass
class SjtuUndergradClient:
    """基于 Cookie 的教学信息服务网本科课表客户端。"""

    cookies_path: Path

    async def fetch_courses(self, year: str, term: str) -> List[Dict[str, Any]]:
        """获取指定学年学期的 kbList 课程数组。"""
        if not self.cookies_path.exists():
            raise SjtuJwClientError(f"Cookie 文件不存在: {self.cookies_path}")

        try:
            raw = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - 极端格式错误路径
            raise SjtuJwClientError("Cookie 文件解析失败，请检查 JSON 格式") from exc

        cookies = httpx.Cookies()

        # 支持 Playwright 导出的 cookies 列表形式，或简单的 name->value 映射
        if isinstance(raw, list):
            for c in raw:
                name = c.get("name")
                value = c.get("value")
                if not name or value is None:
                    continue
                domain_val = c.get("domain")
                domain = str(domain_val) if domain_val is not None else ""
                path = c.get("path") or "/"
                cookies.set(name, value, domain=domain, path=path)
        elif isinstance(raw, dict):
            for name, value in raw.items():
                if value is None:
                    continue
                cookies.set(name, value)
        else:  # pragma: no cover - 极端格式错误路径
            raise SjtuJwClientError("Cookie 文件格式不支持，应为列表或对象")

        xqm = _convert_term_to_xqm(term)

        async with httpx.AsyncClient(cookies=cookies, timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(
                SJTU_UNDERGRAD_COURSE_URL,
                data={"xnm": year, "xqm": xqm},
            )

        text = resp.text
        # 未登录时通常会返回 jAccount 登录页或重定向提示
        if "jaccount" in text.lower():
            raise SjtuJwClientError("NOT_LOGGED_IN")

        try:
            data = resp.json()
        except Exception as exc:  # pragma: no cover - 仅在服务端异常时触发
            raise SjtuJwClientError("UNEXPECTED_RESPONSE") from exc

        kb_list = data.get("kbList", [])
        if not isinstance(kb_list, list):
            raise SjtuJwClientError("UNEXPECTED_RESPONSE")

        return kb_list


def _normalize_course_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """将教学信息服务网的单条课程记录压平为更易用的结构。"""
    jcs = str(item.get("jcs", "")).strip()
    start_section: Optional[int] = None
    section_count: Optional[int] = None
    if "-" in jcs:
        try:
            start_s, end_s = jcs.split("-", 1)
            start_section = int(start_s)
            end_section = int(end_s)
            section_count = max(1, end_section - start_section + 1)
        except Exception:
            start_section = None
            section_count = None

    return {
        "course_id": item.get("kch_id"),
        "class_id": item.get("jxbmc"),
        "course_name": item.get("kcmc"),
        "location": item.get("cdmc") or "",
        "day_of_week": item.get("xqj"),  # 1-7
        "sections_raw": jcs,
        "start_section": start_section,
        "section_count": section_count,
        "teacher": item.get("xm"),
        "weeks_raw": item.get("zcd"),
        "note": item.get("xkbz") or "",
        "raw": item,
    }


class FetchSjtuUndergradScheduleTool(BaseTool):
    """从教学信息服务网获取本科生课表的工具（只读，不直接写入本地日程）。"""

    def __init__(
        self,
        cookies_path: Optional[str] = None,
        client_factory: Callable[[Path], SjtuUndergradClient] = lambda p: SjtuUndergradClient(
            cookies_path=p
        ),
        config: Optional[SjtuJwConfig] = None,
        login_helper: Optional[Callable[[Path], Awaitable[None]]] = None,
    ):
        self._explicit_cookies_path = Path(cookies_path) if cookies_path else None
        self._client_factory = client_factory
        self._config = config
        self._login_helper = login_helper or _default_login_helper

    @property
    def name(self) -> str:
        return "fetch_sjtu_undergrad_schedule"

    def _resolve_cookies_path(self) -> Path:
        if self._explicit_cookies_path is not None:
            return self._explicit_cookies_path
        if self._config is not None and self._config.cookies_path:
            return Path(self._config.cookies_path)
        return Path("./data/sjtu_jw_cookies.json")

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "从上海交通大学教学信息服务网同步本科生课表 (kbList)。\n\n"
                "本工具支持两种方式获取登录 Cookie：\n"
                "1. 自动方式：在调用时传入 refresh_cookies=true，工具会尝试使用 Playwright 打开浏览器，"
                "   让你完成 jAccount 登录，并自动将 Cookie 保存到 config.sjtu_jw.cookies_path 指定的 JSON 文件；\n"
                "2. 手动方式：你也可以在浏览器中登录并导出 Cookie 为 JSON 文件，保存到 cookies_path，"
                "   然后不设置 refresh_cookies 直接使用。\n\n"
                "本工具只负责读取远端课表并返回结构化数据，不直接写入本地日程；"
                "你可以在拿到返回结果后，再决定如何转换为具体日程事件。"
            ),
            parameters=[
                ToolParameter(
                    name="year",
                    type="string",
                    description="学年，使用学年开始年份，如 2025 表示 2025-2026 学年。不传则自动根据当前日期推断。",
                    required=False,
                ),
                ToolParameter(
                    name="term",
                    type="string",
                    description='学期："1"=第一学期，"2"=第二学期，"3"=短学期。不传则自动根据当前日期推断。',
                    required=False,
                ),
                ToolParameter(
                    name="refresh_cookies",
                    type="boolean",
                    description=(
                        "是否在检测到未登录/Cookie 缺失时自动尝试通过 Playwright 打开浏览器完成登录并更新 Cookie。"
                        "默认 false，仅在需要时显式设为 true。"
                    ),
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "同步当前学年的当前学期课表",
                    "params": {"refresh_cookies": True},
                },
                {
                    "description": "同步 2025-2026 学年第一学期课表",
                    "params": {"year": "2025", "term": "1", "refresh_cookies": True},
                },
            ],
            usage_notes=[
                "推荐在第一次使用或 Cookie 过期时，将 refresh_cookies 设置为 true，"
                "让工具自动通过 Playwright 打开浏览器完成 jAccount 登录并更新 Cookie。",
                "如果当前环境无法运行 Playwright，可以改为手动在浏览器中登录并导出 Cookie 为 JSON 文件，"
                "保存到 config.sjtu_jw.cookies_path 指定的位置。",
                "Cookie 建议以 Playwright cookies.json 形式保存，或简单的 name->value 映射对象。",
                "登录状态过期时会返回 NOT_LOGGED_IN 错误，此时可再次使用 refresh_cookies=true 触发自动登录刷新。",
                "本工具返回的每条课程记录都包含原始 raw 字段，可在需要时手动扩展解析逻辑。",
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_year = kwargs.get("year")
        raw_term = kwargs.get("term")
        refresh_cookies = bool(kwargs.get("refresh_cookies") or False)

        if raw_year:
            year = str(raw_year).strip()
        else:
            year, _auto_term = guess_academic_year_and_term()

        if raw_term:
            term = str(raw_term).strip()
        else:
            _auto_year, term = guess_academic_year_and_term()

        cookies_path = self._resolve_cookies_path()
        client = self._client_factory(cookies_path)

        def _build_error_result(error_code: str, detail: Optional[str] = None) -> ToolResult:
            message = "同步课表失败"
            if error_code == "NOT_LOGGED_IN":
                message = (
                    "尚未登录教学信息服务网或登录状态已过期。"
                    "请在浏览器/Playwright 中重新登录教学信息服务网，并更新 Cookie 文件后重试。"
                )
            elif error_code == "UNEXPECTED_RESPONSE":
                message = "教学信息服务网返回了无法解析的响应，请稍后重试。"
            elif error_code == "PLAYWRIGHT_NOT_AVAILABLE":
                message = (
                    "当前环境未安装 Playwright 或浏览器，无法自动打开网页完成登录。"
                    "请先手动登录教学信息服务网并导出 Cookie，或安装 playwright+浏览器后重试。"
                )
            elif error_code == "PLAYWRIGHT_LOGIN_FAILED":
                base_msg = (
                    "Playwright 登录流程失败（可能是无图形界面、网络问题或超时）。"
                    "建议在本地浏览器登录 i.sjtu.edu.cn 并导出 Cookie 为 JSON，"
                    "保存到 config.sjtu_jw.cookies_path 后直接调用本工具。"
                )
                message = f"{base_msg}\n原始错误: {detail}" if detail else base_msg
            elif error_code.startswith("Cookie 文件不存在"):
                error_code = "COOKIES_NOT_FOUND"
                message = (
                    f"未找到 Cookie 文件: {cookies_path}。"
                    "请先登录教学信息服务网并导出 Cookie 为 JSON 文件。"
                )

            return ToolResult(
                success=False,
                error=error_code,
                message=message,
                data={"year": year, "term": term},
            )

        # 第一次尝试获取课表
        try:
            courses_raw = await client.fetch_courses(year=year, term=term)
        except SjtuJwClientError as exc:
            error_code = str(exc)

            # 如有需要，先尝试通过浏览器刷新登录状态，然后重试一次
            should_try_login = refresh_cookies and (
                error_code == "NOT_LOGGED_IN" or error_code.startswith("Cookie 文件不存在")
            )
            if should_try_login:
                try:
                    await self._login_helper(cookies_path)
                except SjtuJwClientError as login_exc:
                    return _build_error_result(str(login_exc), getattr(login_exc, "detail", None))

                # 使用最新 Cookie 重建 client 再试一次
                client = self._client_factory(cookies_path)
                try:
                    courses_raw = await client.fetch_courses(year=year, term=term)
                except SjtuJwClientError as exc2:
                    return _build_error_result(str(exc2))
            else:
                return _build_error_result(error_code)

        normalized = [_normalize_course_item(item) for item in courses_raw]

        return ToolResult(
            success=True,
            message=f"成功获取 {len(normalized)} 条本科课程记录",
            data={
                "year": year,
                "term": term,
                "fetched_at": datetime.utcnow().isoformat(),
                "course_count": len(normalized),
                "courses": normalized,
            },
        )


async def _default_login_helper(cookies_path: Path) -> None:
    """使用 Playwright 打开浏览器完成 jAccount 登录并保存 Cookie。

    用户需要在弹出的浏览器窗口中手动完成登录操作，直到跳转到
    包含 index_initMenu.html 的页面为止。
    """
    ap = _get_async_playwright()
    if ap is None:
        raise SjtuJwClientError("PLAYWRIGHT_NOT_AVAILABLE")

    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        async with ap() as p:  # type: ignore[func-returns-value]
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(SJTU_LOGIN_URL)
            # 等待用户在浏览器中完成 jAccount 登录
            await page.wait_for_url(f"**/{SJTU_AFTER_LOGIN_PATTERN}*", timeout=5 * 60 * 1000)

            cookies = await context.cookies()
            cookies_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")

            await browser.close()
    except Exception as exc:  # pragma: no cover - 依赖宿主环境图形/依赖库
        # 区分「未安装」与「运行时错误」，避免误导用户
        if isinstance(exc, (ImportError, ModuleNotFoundError)):
            raise SjtuJwClientError("PLAYWRIGHT_NOT_AVAILABLE") from exc
        raise SjtuJwClientError("PLAYWRIGHT_LOGIN_FAILED", detail=str(exc)) from exc


