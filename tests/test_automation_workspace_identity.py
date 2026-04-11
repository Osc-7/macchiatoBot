"""automation_daemon：无 memory_owner 时 cron:job 不得含冒号地作为 frontend。"""

from automation_daemon import _workspace_frontend_user_for_automation_task


def test_no_owner_cron_source_uses_shared_automation_dir() -> None:
    fe, uid = _workspace_frontend_user_for_automation_task(
        raw_owner="",
        task_source="cron:job-config-8e541b95",
        task_user_id="default",
    )
    assert fe == "cron"
    assert uid == "_automation"


def test_memory_owner_feishu_passthrough() -> None:
    fe, uid = _workspace_frontend_user_for_automation_task(
        raw_owner="feishu:ou_abc123",
        task_source="cron:ignored",
        task_user_id="default",
    )
    assert fe == "feishu"
    assert uid == "ou_abc123"


def test_unknown_source_with_colon_replaced() -> None:
    fe, uid = _workspace_frontend_user_for_automation_task(
        raw_owner="",
        task_source="weird:thing",
        task_user_id="u1",
    )
    assert fe == "weird_thing"
    assert uid == "u1"
