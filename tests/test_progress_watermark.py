"""裁撤进度水线：同源判定、跳数拦截、互斥分线、串行落库。"""
import sqlite3
import threading

import pytest

from app.db import get_db
from app.services.progress import (
    advance_progress,
    annotate_judgments,
    annotate_partitions,
    build_card,
)


def _execute_all(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    items = authed.get("/api/judgments", params={"status": "待执行"}).json()["items"]
    for item in items:
        assert authed.post(f"/api/judgments/{item['id']}/execute").status_code == 200
    return items


def test_watermark_blocked_until_earliest_expired_done(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    # 一条都不执行：最早已到期分区未齐，水线一步也不许走
    r = authed.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    )
    assert r.status_code == 409
    assert "跳数" in r.json()["detail"] or "未齐" in r.json()["detail"]

    card = authed.get(
        "/api/progress-cards/orders", params={"as_of_date": "2026-11-16"}
    ).json()["card"]
    assert card["watermark_date"] is None
    assert card["candidate_date"] is None
    assert card["can_advance"] is False
    assert card["unfinished_count"] >= 1

    # 失败也要留痕，供本机核账
    logs = authed.get("/api/progress-logs", params={"domain_code": "orders"}).json()[
        "items"
    ]
    assert logs and logs[0]["ok"] == 0
    assert logs[0]["message"]


def test_watermark_advances_after_all_expired_done(authed):
    _execute_all(authed)
    r = authed.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["advanced"] is True
    assert body["watermark_date"] == "2026-11-05"
    assert body["completed_count"] == 2
    card = body["card"]
    assert card["watermark_seq"] == 1
    assert card["unfinished_count"] == 0
    assert card["watermark_completed"] == card["completed_count"] == 2


def test_watermark_no_double_write_on_repeat(authed):
    _execute_all(authed)
    first = authed.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    ).json()
    second = authed.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    ).json()
    assert first["advanced"] is True
    assert second["advanced"] is False
    assert second["message"].find("无新增") >= 0
    marks = authed.get("/api/watermarks", params={"domain_code": "orders"}).json()[
        "items"
    ]
    assert len(marks) == 1


def test_tamper_breaks_same_source_and_blocks(authed):
    _execute_all(authed)
    authed.post("/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"})

    # 人为把已归档分区改回在线（裁决留痕还在）：实物与裁决立刻不同源
    with get_db() as conn:
        part = conn.execute(
            "SELECT id FROM partitions WHERE domain_code='orders' AND biz_date='2026-11-01'"
        ).fetchone()
        conn.execute(
            "UPDATE partitions SET storage_state='在线', archive_uri=NULL WHERE id=?",
            (part["id"],),
        )

    card = authed.get(
        "/api/progress-cards/orders", params={"as_of_date": "2026-11-16"}
    ).json()["card"]
    assert card["anomaly_count"] >= 1
    assert card["can_advance"] is False
    assert "不同源" in card["block_reason"] or "账实" in card["block_reason"]

    # 再推进必须被拦，且零新增快照
    r = authed.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    )
    assert r.status_code == 409
    marks = authed.get("/api/watermarks", params={"domain_code": "orders"}).json()[
        "items"
    ]
    assert len(marks) == 1


def test_partition_list_and_judgment_queue_share_watermark(authed):
    _execute_all(authed)
    authed.post("/api/progress-cards/events/advance", json={"as_of_date": "2026-11-16"})

    parts = authed.get(
        "/api/partitions", params={"as_of_date": "2026-11-16"}
    ).json()["items"]
    events = [p for p in parts if p["domain_code"] == "events"]
    assert events
    expired = [p for p in events if p["should_be_done"]]
    assert expired and len(expired) < len(events)  # 11-15 那条尚未到期
    assert all(p["in_watermark"] for p in expired)
    assert all(p["progress_state"] == "水线内已齐" for p in expired)
    assert all(
        p["progress_state"] == "未到期" for p in events if not p["should_be_done"]
    )

    judgments = authed.get(
        "/api/judgments", params={"as_of_date": "2026-11-16"}
    ).json()["items"]
    events_j = [j for j in judgments if j["domain_code"] == "events"]
    assert events_j
    assert all(j["progress_state"] == "水线内已完成" for j in events_j)

    # 水线内应裁未裁：把其中一条改回在线后，分区列表必须亮红
    with get_db() as conn:
        part = conn.execute(
            "SELECT id FROM partitions WHERE domain_code='events' "
            "AND biz_date='2026-11-08'"
        ).fetchone()
        conn.execute(
            "UPDATE partitions SET storage_state='在线', row_count=5000, "
            "archive_uri=NULL WHERE id=?",
            (part["id"],),
        )
    parts = authed.get(
        "/api/partitions", params={"as_of_date": "2026-11-16"}
    ).json()["items"]
    bad = [
        p
        for p in parts
        if p["domain_code"] == "events" and p["progress_state"] == "水线内应裁未裁"
    ]
    assert len(bad) == 1
    assert "账实已偏" in bad[0]["progress_note"]

    # 应毁分区也必须在卡片上可见
    card = authed.get(
        "/api/progress-cards/events", params={"as_of_date": "2026-11-16"}
    ).json()["card"]
    assert card["action_on_expire"] == "销毁"
    assert card["anomaly_count"] >= 1
    assert card["destroy_pending_count"] >= 1


def test_action_switch_freezes_old_line(authed):
    _execute_all(authed)
    authed.post("/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"})

    # 策略动作从「归档」改为「销毁」并换版：旧线封存，新线从零起
    with get_db() as conn:
        conn.execute(
            "UPDATE retain_policies SET active=0 WHERE domain_code='orders'"
        )
        conn.execute(
            "INSERT INTO retain_policies "
            "(domain_code, domain_name, keep_days, action_on_expire, version, active) "
            "VALUES ('orders','订单明细域',7,'销毁',2,1)"
        )

    card = authed.get(
        "/api/progress-cards/orders", params={"as_of_date": "2026-11-16"}
    ).json()["card"]
    assert card["action_on_expire"] == "销毁"
    assert card["watermark_date"] is None
    assert card["frozen_lines"]
    frozen = card["frozen_lines"][0]
    assert frozen["action_on_expire"] == "归档"
    assert frozen["watermark_date"] == "2026-11-05"
    # 归档实物不能冒充销毁进度：全部应按销毁重算为未齐
    assert card["watermark_completed"] == 0
    assert card["can_advance"] is False


def test_concurrent_advance_is_serialized(authed):
    _execute_all(authed)
    db_path = None
    with get_db() as conn:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            barrier.wait()
            out = advance_progress(conn, "orders", "2026-11-16")
            with lock:
                results.append(out)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    # 串行后第二个只能看到已推进的水线，不得双写
    assert sum(1 for r in results if r["advanced"]) == 1
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM watermark_snapshots "
            "WHERE domain_code='orders' AND action_on_expire='归档'"
        ).fetchone()["c"]
    assert n == 1


def test_snapshot_is_immutable(authed):
    _execute_all(authed)
    authed.post("/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"})
    with get_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE watermark_snapshots SET completed_count = 999 "
                "WHERE domain_code='orders'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM watermark_snapshots WHERE domain_code='orders'")


def test_advance_requires_login(client):
    r = client.post(
        "/api/progress-cards/orders/advance", json={"as_of_date": "2026-11-16"}
    )
    assert r.status_code == 401


def test_card_rejects_bad_as_of(authed):
    r = authed.get("/api/progress-cards/orders", params={"as_of_date": "昨天"})
    assert r.status_code == 400
