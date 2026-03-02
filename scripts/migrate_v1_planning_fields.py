#!/usr/bin/env python3
"""
为 events.json / tasks.json 补齐 v1 规划字段。

用法：
    python scripts/migrate_v1_planning_fields.py --data-dir ./data
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


EVENT_DEFAULTS = {
    "source": "user",
    "event_type": "normal",
    "is_blocking": True,
    "origin_ref": None,
    "linked_task_id": None,
    "plan_run_id": None,
    "metadata": {},
}

TASK_DEFAULTS = {
    "difficulty": 3,
    "importance": 3,
    "source": "user",
    "origin_ref": None,
    "deadline_event_id": None,
    "metadata": {},
}


def _migrate_file(path: Path, defaults: dict) -> tuple[int, int]:
    if not path.exists():
        return 0, 0

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        return 0, 0

    touched = 0
    total = 0
    for key, item in raw.items():
        if not isinstance(item, dict):
            continue
        total += 1
        changed = False
        for field, default in defaults.items():
            if field not in item:
                item[field] = default
                changed = True
        if changed:
            touched += 1
        raw[key] = item

    if touched:
        with path.open("w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    return total, touched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data", help="数据目录")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    events_path = data_dir / "events.json"
    tasks_path = data_dir / "tasks.json"

    e_total, e_touched = _migrate_file(events_path, EVENT_DEFAULTS)
    t_total, t_touched = _migrate_file(tasks_path, TASK_DEFAULTS)

    print(
        "migration_done",
        {
            "events_total": e_total,
            "events_updated": e_touched,
            "tasks_total": t_total,
            "tasks_updated": t_touched,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

