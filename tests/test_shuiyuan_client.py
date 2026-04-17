import time

import pytest

from frontend.shuiyuan_integration.client import (
    ShuiyuanClient,
    ShuiyuanClientPool,
    _plain_from_discourse_cooked,
    extract_rate_limit_wait_seconds,
)


def test_update_post_put_json_body(monkeypatch):
    calls = []

    class _Ok:
        status_code = 200

        def json(self):
            return {"id": 42, "raw": "新正文"}

    def _fake_put(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return _Ok()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr("frontend.shuiyuan_integration.client.requests.put", _fake_put)

    client = ShuiyuanClient(user_api_key="k")
    result, status, detail = client.update_post(42, "新正文", edit_reason="fix")

    assert status == 200
    assert detail == ""
    assert result == {"id": 42, "raw": "新正文"}
    assert len(calls) == 1
    assert calls[0][0].endswith("/posts/42.json")
    assert calls[0][1] == {
        "post": {"raw": "新正文", "edit_reason": "fix"},
    }


def test_get_post_by_id_403_returns_none(monkeypatch):
    class _Forbidden:
        status_code = 403
        text = ""

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get",
        lambda *a, **k: _Forbidden(),
    )
    assert ShuiyuanClient(user_api_key="k").get_post_by_id(1, 99999) is None


def test_get_post_with_read_fallback_topic_posts_when_posts_json_403(monkeypatch):
    class R403:
        status_code = 403
        text = "{}"

    class R200:
        status_code = 200

        def json(self):
            return {
                "post_stream": {
                    "posts": [
                        {
                            "id": 42,
                            "raw": "hi",
                            "username": "Osc7",
                            "post_number": 2,
                        }
                    ]
                }
            }

    def fake_get(url, **kwargs):
        u = str(url)
        if "/posts/42.json" in u or u.endswith("/posts/42.json"):
            return R403()
        if "/t/100/posts.json" in u:
            return R200()
        return R403()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr("frontend.shuiyuan_integration.client.requests.get", fake_get)
    post = ShuiyuanClient(user_api_key="k").get_post_with_read_fallback(100, 42)
    assert post is not None
    assert post.get("raw") == "hi"


def test_plain_from_discourse_cooked_strips_tags():
    html = '<p><a class="mention">@Osc7</a> 【玛奇朵】</p>'
    t = _plain_from_discourse_cooked(html)
    assert "Osc7" in t
    assert "【玛奇朵】" in t


def test_pool_get_post_with_read_fallback_tries_next_key_on_none(monkeypatch, tmp_path):
    """第一把 Key 读帖返回 None 时换下一 Key（不依赖通用 _call 单次选 Key）。"""

    def fake_gprf(self: ShuiyuanClient, tid: int, pid: int):
        k = (self._headers or {}).get("User-Api-Key") or ""
        if k == "bad":
            return None
        if k == "good":
            return {"id": pid, "raw": "ok", "username": "u"}
        return None

    monkeypatch.setattr(
        ShuiyuanClient,
        "get_post_with_read_fallback",
        fake_gprf,
    )
    pool = ShuiyuanClientPool(
        ["bad", "good"],
        state_path=tmp_path / "s.json",
        stale_probe_extra_gap_seconds=0.0,
    )
    post = pool.get_post_with_read_fallback(100, 42)
    assert post is not None
    assert post.get("raw") == "ok"


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_toggle_retort_prefers_put_retorts_without_json(monkeypatch):
    calls = []

    def _fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Resp(200)

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.request", _fake_request
    )

    client = ShuiyuanClient(user_api_key="k")
    ok, status, detail = client.toggle_retort(post_id=123, emoji="thumbsup")

    assert ok is True
    assert status == 200
    assert detail == ""
    assert len(calls) == 1
    # 优先使用 ShuiyuanSJTU/retort 的 PUT /retorts/:post_id（无 .json）
    assert calls[0][0] == "PUT"
    assert calls[0][1].endswith("/retorts/123")
    assert calls[0][2]["data"]["retort"] == "thumbsup"


def test_toggle_retort_fallbacks_from_json_to_legacy_retorts(monkeypatch):
    calls = []
    # 先尝试无 .json（404），再尝试 .json（201）
    responses = [_Resp(404, "not found"), _Resp(201)]

    def _fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.request", _fake_request
    )

    client = ShuiyuanClient(user_api_key="k")
    ok, status, detail = client.toggle_retort(post_id=456, emoji="heart")

    assert ok is True
    assert status == 201
    assert detail == ""
    assert len(calls) == 2
    # 第一次：PUT /retorts/456（无 .json）
    assert calls[0][1].endswith("/retorts/456")
    # 第二次：PUT /retorts/456.json（fallback）
    assert calls[1][1].endswith("/retorts/456.json")


def test_toggle_retort_fallbacks_to_discourse_reactions(monkeypatch):
    calls = []
    # 依次尝试：
    # 1) PUT /retorts/:id
    # 2) PUT /retorts/:id.json
    # 3) POST /retorts/:id.json
    # 4) POST /retorts/:id
    # 5) POST /discourse-reactions/.../toggle.json
    responses = [
        _Resp(404),
        _Resp(404),
        _Resp(404),
        _Resp(404),
        _Resp(204),
    ]

    def _fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.request", _fake_request
    )

    client = ShuiyuanClient(user_api_key="k")
    ok, status, detail = client.toggle_retort(post_id=789, emoji="+1")

    assert ok is True
    assert status == 204
    assert detail == ""
    assert len(calls) == 5
    # 最终回退到 discourse-reactions 插件的 POST /discourse-reactions/.../toggle.json
    assert calls[-1][0] == "POST"
    assert (
        "/discourse-reactions/posts/789/custom-reactions/%2B1/toggle.json"
        in calls[-1][1]
    )


def test_get_latest_topics(monkeypatch):
    """测试获取最新话题列表API"""
    calls = []

    fake_response = {
        "topic_list": {
            "topics": [
                {"id": 1, "title": "Test 1", "posts_count": 10},
                {"id": 2, "title": "Test 2", "posts_count": 5},
            ],
            "more_topics_url": "/latest?page=1",
        },
        "users": [{"id": 1, "username": "user1"}],
    }

    class _FakeResp:
        status_code = 200

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeResp()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", _fake_get
    )

    client = ShuiyuanClient(user_api_key="k")
    result = client.get_latest_topics(page=0, per_page=30)

    assert result["topic_list"]["topics"][0]["id"] == 1
    assert calls[0][0].endswith("/latest.json")
    assert calls[0][1]["page"] == 0


def test_get_top_topics(monkeypatch):
    """测试获取热门话题列表API"""
    calls = []

    fake_response = {
        "topic_list": {
            "topics": [
                {"id": 1, "title": "Hot Topic", "views": 1000},
            ],
        },
        "users": [],
    }

    class _FakeResp:
        status_code = 200

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeResp()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", _fake_get
    )

    client = ShuiyuanClient(user_api_key="k")
    result = client.get_top_topics(period="weekly", page=0)

    assert result["topic_list"]["topics"][0]["views"] == 1000
    assert "/top/weekly.json" in calls[0][0]


def test_get_categories(monkeypatch):
    """测试获取类别列表API"""
    calls = []

    fake_response = {
        "category_list": {
            "categories": [
                {
                    "id": 1,
                    "name": "水源开发者",
                    "description": "开发讨论",
                    "topic_count": 100,
                },
            ],
        },
    }

    class _FakeResp:
        status_code = 200

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeResp()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", _fake_get
    )

    client = ShuiyuanClient(user_api_key="k")
    result = client.get_categories(include_subcategories=True)

    assert result["category_list"]["categories"][0]["name"] == "水源开发者"
    assert "/categories.json" in calls[0][0]


def test_get_category_topics(monkeypatch):
    """测试获取特定类别下的话题列表API"""
    calls = []

    fake_response = {
        "topic_list": {
            "topics": [
                {"id": 1, "title": "Category Topic", "posts_count": 5},
            ],
        },
        "users": [],
    }

    class _FakeResp:
        status_code = 200

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    def _fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {})))
        return _FakeResp()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", _fake_get
    )

    client = ShuiyuanClient(user_api_key="k")
    result = client.get_category_topics(category_id=42, page=0)

    assert result["topic_list"]["topics"][0]["title"] == "Category Topic"
    assert "/c/42.json" in calls[0][0]


def test_get_topic_posts_paged(monkeypatch):
    """测试翻页获取话题帖子API"""
    calls = []

    topic_response = {
        "id": 123,
        "title": "Test Topic",
        "post_stream": {
            "stream": [101, 102, 103, 104, 105, 106, 107, 108, 109, 110],
        },
    }

    posts_response = {
        "post_stream": {
            "posts": [
                {"id": 101, "post_number": 1, "raw": "First post"},
                {"id": 102, "post_number": 2, "raw": "Second post"},
            ],
        },
    }

    class _FakeResp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def _fake_get(url, **kwargs):
        calls.append(url)
        if "/t/123.json" in url:
            return _FakeResp(topic_response)
        return _FakeResp(posts_response)

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", _fake_get
    )

    client = ShuiyuanClient(user_api_key="k")
    result = client.get_topic_posts_paged(topic_id=123, post_number_start=1, limit=5)

    assert len(result) == 2
    assert result[0]["post_number"] == 1
    assert result[1]["post_number"] == 2


def test_check_user_api_key_session_ok(monkeypatch):
    def fake_get(url, **kwargs):
        class R:
            status_code = 200

            def json(self):
                return {"current_user": {"id": 1, "username": "u"}}

        return R()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", fake_get
    )
    assert ShuiyuanClient(user_api_key="k").check_user_api_key_session() is True


def test_check_user_api_key_session_false_on_non_200(monkeypatch):
    def fake_get(url, **kwargs):
        class R:
            status_code = 429

        return R()

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", fake_get
    )
    assert ShuiyuanClient(user_api_key="k").check_user_api_key_session() is False


def test_pool_clears_stale_lockout_when_session_probe_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.ShuiyuanClient.check_user_api_key_session",
        lambda self: True,
    )
    state = tmp_path / "st.json"
    pool = ShuiyuanClientPool(
        ["a", "b"],
        state_path=state,
        stale_probe_extra_gap_seconds=0.0,
    )
    pool._stale_lockout_probe_interval_seconds = 0.0
    far = time.time() + 99999.0
    pool._blocked_until["a"] = far
    pool._blocked_until["b"] = far
    key = pool._select_key()
    assert key in ("a", "b")
    assert pool._blocked_until["a"] == 0.0
    assert pool._blocked_until["b"] == 0.0


def test_pool_stale_probe_throttled(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_check(self):
        calls["n"] += 1
        return False

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.ShuiyuanClient.check_user_api_key_session",
        fake_check,
    )
    pool = ShuiyuanClientPool(
        ["x", "y"],
        state_path=tmp_path / "s.json",
        stale_probe_extra_gap_seconds=0.0,
    )
    far = time.time() + 99999.0
    pool._blocked_until["x"] = far
    pool._blocked_until["y"] = far
    with pytest.raises(RuntimeError, match="已在冷却中"):
        pool._select_key()
    assert calls["n"] == 2
    with pytest.raises(RuntimeError, match="已在冷却中"):
        pool._select_key()
    assert calls["n"] == 2


def test_extract_rate_limit_wait_seconds_retry_after():
    assert extract_rate_limit_wait_seconds({"Retry-After": "120"}, "") == 120
    assert extract_rate_limit_wait_seconds({"retry-after": "60"}, "") == 60


def test_extract_rate_limit_wait_seconds_extras_body():
    body = '{"extras": {"wait_seconds": 300}}'
    assert extract_rate_limit_wait_seconds({}, body) == 300
    body2 = '{"extras": {"waitSeconds": 45}}'
    assert extract_rate_limit_wait_seconds({}, body2) == 45


def test_probe_user_api_key_health_429_parses_wait(monkeypatch):
    class R:
        status_code = 429
        headers = {
            "Retry-After": "42",
            "discourse-rate-limit-error-code": "user_api_key_limiter_1_day",
        }
        text = "{}"

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.requests.get", lambda *a, **k: R()
    )
    pr = ShuiyuanClient(user_api_key="k").probe_user_api_key_health("Osc7")
    assert pr["ok"] is False
    assert pr["status_code"] == 429
    assert pr["wait_seconds"] == 42
    assert "user_api_key" in (pr.get("rate_limit_code") or "")


def test_pool_probe_user_actions_429_updates_local_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client._ensure_rate_limit", lambda: None
    )

    def fake_probe(self, username: str):
        assert username == "u1"
        return {
            "ok": False,
            "status_code": 429,
            "wait_seconds": 90,
            "rate_limit_code": "user_api_key_limiter_1_day",
        }

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.client.ShuiyuanClient.probe_user_api_key_health",
        fake_probe,
    )
    pool = ShuiyuanClientPool(
        ["k1"],
        state_path=tmp_path / "st.json",
        probe_username="u1",
        stale_probe_extra_gap_seconds=0.0,
    )
    pool._stale_lockout_probe_interval_seconds = 0.0
    pool._blocked_until["k1"] = time.time() + 99999.0
    with pytest.raises(RuntimeError, match="已在冷却中"):
        pool._select_key()
    bu = pool._blocked_until["k1"]
    assert bu <= time.time() + 95
    assert bu >= time.time() + 85
