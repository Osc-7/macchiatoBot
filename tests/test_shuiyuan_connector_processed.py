"""connector：post_id 持久化去重（防止通知水位断档重复回复）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from frontend.shuiyuan_integration import connector as M


def test_processed_save_keeps_largest_ids(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(M, "_MAX_PROCESSED_POST_IDS", 3)
    M._save_processed_post_ids({1, 2, 3, 4, 5})
    loaded = M._load_processed_post_ids()
    assert loaded == {3, 4, 5}


def test_seed_processed_from_notify_stream(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "seed.json")

    seeded = M._seed_processed_post_ids_from_existing_state(stream_list=[10, 11, 12])

    assert seeded == {10, 11, 12}
    assert M._load_processed_post_ids() == {10, 11, 12}


def test_seed_processed_from_stream_map(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "seed-map.json")

    seeded = M._seed_processed_post_ids_from_existing_state(
        stream_map={1: {100, 101}, 2: {200}}
    )

    assert seeded == {100, 101, 200}
    assert M._load_processed_post_ids() == {100, 101, 200}


def test_seed_processed_does_not_overwrite_existing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "existing.json")
    M._save_processed_post_ids({1})

    seeded = M._seed_processed_post_ids_from_existing_state(stream_list=[2, 3])

    assert seeded == {1}
    assert M._load_processed_post_ids() == {1}


@pytest.mark.asyncio
async def test_poll_once_skips_when_post_id_already_processed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "p.json")
    M._save_processed_post_ids({1000})

    def fake_collect(client, cfg):
        return [(10, 5, 1000), (10, 4, 999)]

    monkeypatch.setattr(M, "_collect_mention_post_ids", fake_collect)

    cfg = SimpleNamespace(shuiyuan=SimpleNamespace(owner_username="u"))
    pending: set = set()
    sem = asyncio.Semaphore(6)

    await M._poll_once(
        client=None,
        config=cfg,
        stream_list=[999],
        reply_sem=sem,
        pending_tasks=pending,
    )

    assert len(pending) == 0


@pytest.mark.asyncio
async def test_poll_once_schedules_unseen_post_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "_PROCESSED_POST_IDS_PATH", tmp_path / "e.json")

    scheduled: list[object] = []

    def stub_schedule(aw, *, pending_tasks=None) -> None:
        scheduled.append(1)
        try:
            aw.close()
        except Exception:
            pass

    monkeypatch.setattr(M, "_schedule_background_reply", stub_schedule)

    def fake_collect(client, cfg):
        return [(10, 5, 1000), (10, 4, 999)]

    monkeypatch.setattr(M, "_collect_mention_post_ids", fake_collect)

    cfg = SimpleNamespace(shuiyuan=SimpleNamespace(owner_username="u"))

    await M._poll_once(
        client=None,
        config=cfg,
        stream_list=[999],
        reply_sem=asyncio.Semaphore(6),
        pending_tasks=None,
    )

    assert len(scheduled) == 1
