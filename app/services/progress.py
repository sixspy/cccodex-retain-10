"""裁撤进度水线。

落库形态：**独立水位快照表** ``watermark_snapshots``（append-only、不可变）。
- 水线身份 = (主题域, 到期动作, 策略版本)。归档 / 销毁互斥，绝不共线；
  策略换动作或换版本即旧线封存，新线从 seq=1 重新起算。
- 推进一次只追加一条快照；同一事务内写成功日志。失败零写入快照，
  另记一条失败日志（progress_advance_logs），并返回中文原因。
- 完成数与分区实物同源：只认 partitions 的最终态 + 同动作已完成裁决留痕，
  不拿裁决条数当进度；两者不一致直接拦截。
- 防跳数：水线只落在「到期分区按业务日连续完成」的前缀末日内；
  快照序号必须 prev+1，完成数必须 = 上条完成数 + 新完成数。
- 并发：BEGIN IMMEDIATE 串行化写事务，另有 UNIQUE 约束兜底；
  拿不到写锁时中文提示，不双写。
"""
from datetime import date, datetime, timedelta

import sqlite3

from app.services.policies import get_active_policy

ACTIONS = ("归档", "销毁")
FINAL_STATE = {"归档": "已归档", "销毁": "已销毁"}


def _parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("基准日格式应为 YYYY-MM-DD") from exc


def _today() -> str:
    return date.today().isoformat()


def _physical_done(action: str, state: str, row_count: int, archive_uri: str | None) -> bool:
    if action == "归档":
        return state == "已归档" and bool(archive_uri)
    if action == "销毁":
        return state == "已销毁" and int(row_count) == 0 and not archive_uri
    return False


def _latest_snapshot(
    conn: sqlite3.Connection,
    domain_code: str,
    action: str,
    policy_version: int,
) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, action_on_expire, policy_version, seq, "
        "watermark_date, completed_count, cutoff_date, as_of_date, created_at "
        "FROM watermark_snapshots "
        "WHERE domain_code = ? AND action_on_expire = ? AND policy_version = ? "
        "ORDER BY seq DESC LIMIT 1",
        (domain_code, action, policy_version),
    ).fetchone()
    return dict(row) if row else None


def _line_snapshots(conn: sqlite3.Connection, domain_code: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, domain_code, action_on_expire, policy_version, seq, "
        "watermark_date, completed_count, cutoff_date, as_of_date, created_at "
        "FROM watermark_snapshots WHERE domain_code = ? "
        "ORDER BY action_on_expire, policy_version, seq",
        (domain_code,),
    ).fetchall()
    return [dict(r) for r in rows]


def _eligible(conn: sqlite3.Connection, policy: dict, as_of: date) -> list[dict]:
    """到期分区清单：只取 partitions 实物，并核对同动作的已完成裁决留痕。"""
    cutoff = (as_of - timedelta(days=int(policy["keep_days"]))).isoformat()
    action = policy["action_on_expire"]
    rows = conn.execute(
        """
        SELECT p.id AS partition_id, p.domain_code, p.biz_date, p.row_count,
               p.storage_state, p.archive_uri,
               (SELECT j.id FROM judgments j
                 WHERE j.partition_id = p.id AND j.status = '已完成'
                   AND j.decided_action = ?
                 ORDER BY j.id DESC LIMIT 1) AS completed_judgment_id,
               (SELECT j.id FROM judgments j
                 WHERE j.partition_id = p.id
                   AND j.status IN ('待执行','执行中')
                 ORDER BY j.id DESC LIMIT 1) AS open_judgment_id
        FROM partitions p
        WHERE p.domain_code = ? AND p.biz_date <= ?
        ORDER BY p.biz_date, p.id
        """,
        (action, policy["domain_code"], cutoff),
    ).fetchall()

    items: list[dict] = []
    for row in rows:
        item = dict(row)
        physical = _physical_done(
            action,
            item["storage_state"],
            item["row_count"],
            item["archive_uri"],
        )
        ledger = item["completed_judgment_id"] is not None
        item["physical_done"] = physical
        item["ledger_done"] = ledger
        item["done"] = physical and ledger
        item["reason"] = _reason(action, item)
        items.append(item)
    return items


def _reason(action: str, item: dict) -> str:
    if item["done"]:
        return ""
    if item["physical_done"] and not item["ledger_done"]:
        return "分区实物已完成，但缺少同动作已完成裁决留痕，账实不符"
    if item["ledger_done"] and not item["physical_done"]:
        return f"裁决已完成，但分区实物仍是「{item['storage_state']}」，账实不符"
    reason = f"分区仍是「{item['storage_state']}」，尚未执行{action}"
    if item["open_judgment_id"]:
        reason += f"（已有进行中裁决 #{item['open_judgment_id']}）"
    return reason


def _audited_done_count(
    conn: sqlite3.Connection,
    domain_code: str,
    action: str,
    upto_date: str,
    cutoff_date: str | None = None,
) -> int:
    """指定日期内、实物最终态与已完成裁决留痕同时成立的实物分区数。"""
    if action == "归档":
        physical = "p.storage_state = '已归档' AND p.archive_uri IS NOT NULL AND p.archive_uri <> ''"
    else:
        physical = (
            "p.storage_state = '已销毁' AND p.row_count = 0 "
            "AND (p.archive_uri IS NULL OR p.archive_uri = '')"
        )
    sql = (
        "SELECT COUNT(*) AS c FROM partitions p "
        "WHERE p.domain_code = ? AND p.biz_date <= ? AND " + physical + " "
        "AND EXISTS (SELECT 1 FROM judgments j WHERE j.partition_id = p.id "
        "AND j.status = '已完成' AND j.decided_action = ?)"
    )
    params: list[object] = [domain_code, upto_date, action]
    if cutoff_date:
        sql += " AND p.biz_date <= ?"
        params.append(cutoff_date)
    return int(conn.execute(sql, params).fetchone()["c"])


def _candidate(eligible: list[dict]) -> tuple[str | None, str | None]:
    """连续完成前缀的末日；返回 (水线候选日, 首个未齐日)。"""
    candidate: str | None = None
    first_undone: str | None = None
    for biz_date in sorted({item["biz_date"] for item in eligible}):
        group = [item for item in eligible if item["biz_date"] == biz_date]
        if all(item["done"] for item in group):
            candidate = biz_date
        else:
            first_undone = biz_date
            break
    return candidate, first_undone


def _frozen_lines(conn: sqlite3.Connection, policy: dict) -> list[dict]:
    """已封存的旧水线（动作或版本与现行策略不同）。"""
    lines: dict[tuple[str, int], dict] = {}
    for snap in _line_snapshots(conn, policy["domain_code"]):
        key = (snap["action_on_expire"], snap["policy_version"])
        lines[key] = snap
    frozen = [
        snap
        for (action, version), snap in lines.items()
        if not (
            action == policy["action_on_expire"]
            and version == int(policy["version"])
        )
    ]
    frozen.sort(key=lambda s: (s["action_on_expire"], s["policy_version"]))
    return frozen


def build_card(
    conn: sqlite3.Connection,
    domain_code: str,
    as_of_date: str | None = None,
) -> dict:
    policy = get_active_policy(conn, domain_code)
    if not policy:
        raise ValueError("主题域不存在现行留存策略，无法查看进度水线")
    as_of_text = (as_of_date or "").strip() or _today()
    as_of = _parse_day(as_of_text)
    action = policy["action_on_expire"]
    version = int(policy["version"])
    cutoff = (as_of - timedelta(days=int(policy["keep_days"]))).isoformat()

    eligible = _eligible(conn, policy, as_of)
    candidate, first_undone = _candidate(eligible)
    snap = _latest_snapshot(conn, domain_code, action, version)
    watermark_date = snap["watermark_date"] if snap else None

    done_total = sum(1 for item in eligible if item["done"])
    # 水线完成数与写入时同一口径：只认 <= 水线且 <= 当时到期线的分区实物
    # + 同动作已完成裁决留痕。用快照自带 cutoff，避免换基准日重算口径。
    done_behind = (
        _audited_done_count(
            conn, domain_code, action, watermark_date, snap["cutoff_date"]
        )
        if watermark_date
        else 0
    )
    unfinished = [item for item in eligible if not item["done"]]
    anomalies = [
        item
        for item in eligible
        if item["physical_done"] != item["ledger_done"]
    ]
    destroy_pending = (
        sum(1 for item in unfinished) if action == "销毁" else 0
    )

    count_mismatch = bool(snap and done_behind != int(snap["completed_count"]))
    can_advance = bool(
        candidate
        and not count_mismatch
        and not anomalies
        and (snap is None or candidate > snap["watermark_date"])
    )

    block_reason = ""
    if anomalies:
        block_reason = f"存在 {len(anomalies)} 条账实不符分区（实物与裁决留痕不同源），水线已拦截"
    elif not eligible:
        block_reason = f"基准日 {as_of_text} 无到期分区，水线无可推进"
    elif candidate is None:
        block_reason = "最早已到期分区尚未完成裁撤，水线不可越过未齐条"
    elif count_mismatch:
        block_reason = (
            "水位完成数与分区实物不同源："
            f"快照记 {snap['completed_count']}，实物 {done_behind}，已拦截"
        )
    elif snap and candidate < snap["watermark_date"]:
        block_reason = f"基准日早于已落库水线 {snap['watermark_date']}，水线不回退"
    elif snap and candidate == snap["watermark_date"]:
        block_reason = f"水线已在 {snap['watermark_date']}，本次无新增完成"

    return {
        "domain_code": domain_code,
        "domain_name": policy["domain_name"],
        "action_on_expire": action,
        "policy_version": version,
        "keep_days": int(policy["keep_days"]),
        "as_of_date": as_of_text,
        "cutoff_date": cutoff,
        "watermark_date": watermark_date,
        "watermark_seq": int(snap["seq"]) if snap else 0,
        "watermark_completed": int(snap["completed_count"]) if snap else 0,
        "watermark_updated_at": snap["created_at"] if snap else "",
        "watermark_as_of": snap["as_of_date"] if snap else "",
        "candidate_date": candidate,
        "completed_count": done_behind,
        "done_total": done_total,
        "eligible_total": len(eligible),
        "unfinished_count": len(unfinished),
        "destroy_pending_count": destroy_pending,
        "anomaly_count": len(anomalies),
        "count_mismatch": count_mismatch,
        "can_advance": can_advance,
        "block_reason": block_reason,
        "first_undone_date": first_undone,
        "unfinished": [
            {
                "partition_id": item["partition_id"],
                "biz_date": item["biz_date"],
                "storage_state": item["storage_state"],
                "row_count": int(item["row_count"]),
                "open_judgment_id": item["open_judgment_id"],
                "physical_done": item["physical_done"],
                "ledger_done": item["ledger_done"],
                "blocks_watermark": bool(
                    first_undone and item["biz_date"] <= first_undone
                ),
                "reason": item["reason"],
            }
            for item in unfinished
        ],
        "frozen_lines": [
            {
                "action_on_expire": line["action_on_expire"],
                "policy_version": line["policy_version"],
                "seq": line["seq"],
                "watermark_date": line["watermark_date"],
                "completed_count": line["completed_count"],
                "as_of_date": line["as_of_date"],
            }
            for line in _frozen_lines(conn, policy)
        ],
        "policy_changed": bool(_frozen_lines(conn, policy)),
    }


def list_cards(conn: sqlite3.Connection, as_of_date: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT DISTINCT domain_code FROM retain_policies WHERE active = 1 "
        "ORDER BY domain_code"
    ).fetchall()
    return [build_card(conn, r["domain_code"], as_of_date) for r in rows]


def _log(
    conn: sqlite3.Connection,
    *,
    domain_code: str,
    action: str,
    policy_version: int,
    as_of_date: str,
    ok: int,
    before_watermark: str | None,
    after_watermark: str | None,
    completed_count: int,
    message: str,
) -> None:
    conn.execute(
        "INSERT INTO progress_advance_logs "
        "(domain_code, action_on_expire, policy_version, as_of_date, ok, "
        "before_watermark, after_watermark, completed_count, message) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            domain_code,
            action,
            policy_version,
            as_of_date,
            ok,
            before_watermark,
            after_watermark,
            completed_count,
            message,
        ),
    )


def _log_failure(
    conn: sqlite3.Connection,
    *,
    domain_code: str,
    action: str,
    policy_version: int,
    as_of_date: str,
    message: str,
) -> None:
    """失败留痕必须独立落库：接口随后抛错会触发外层回滚，不能连日志一起回滚。"""
    prev_isolation = conn.isolation_level
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.isolation_level = None  # 自动提交，避免被外层 get_db 回滚
        _log(
            conn,
            domain_code=domain_code,
            action=action,
            policy_version=policy_version,
            as_of_date=as_of_date,
            ok=0,
            before_watermark=None,
            after_watermark=None,
            completed_count=0,
            message=message,
        )
    except sqlite3.Error:
        # 失败日志本身不遮蔽原始错误。
        pass
    finally:
        conn.isolation_level = prev_isolation


def advance_progress(
    conn: sqlite3.Connection,
    domain_code: str,
    as_of_date: str,
) -> dict:
    """推进一次水线：单事务追加一条不可变快照，失败零写入。"""
    as_of_text = (as_of_date or "").strip()
    as_of = _parse_day(as_of_text)
    policy = get_active_policy(conn, domain_code)
    if not policy:
        raise ValueError("主题域不存在现行留存策略，无法推进水线")
    action = policy["action_on_expire"]
    version = int(policy["version"])
    keep_days = int(policy["keep_days"])

    before_watermark: str | None = None
    after_watermark: str | None = None
    completed_count = 0
    wrote_snapshot = False
    noop = False
    message = ""
    prev_isolation = conn.isolation_level

    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        prev = _latest_snapshot(conn, domain_code, action, version)
        eligible = _eligible(conn, policy, as_of)
        candidate, first_undone = _candidate(eligible)

        if not eligible:
            raise ValueError(f"基准日 {as_of_text} 无到期分区，水线无可推进")
        if candidate is None:
            undone = [item for item in eligible if not item["done"]]
            raise ValueError(
                "到期分区未按业务日连续完成，禁止跳数推进："
                f"未齐 {len(undone)} 条，最早未齐 {first_undone}"
            )
        # 账实不符分区一律不得推进：哪怕它在未齐条之后，也说明实物与留痕已脱节。
        anomalies = [
            item
            for item in eligible
            if item["physical_done"] != item["ledger_done"]
        ]
        if anomalies:
            first = anomalies[0]
            raise ValueError(
                f"存在 {len(anomalies)} 条账实不符分区，水线已拦截："
                f"{first['biz_date']} {first['reason']}"
            )
        # 历史已落库水线内的分区若被改动，也要拦（完成数口径同源复核）。
        if prev:
            already_passed = [
                item
                for item in eligible
                if item["biz_date"] <= prev["watermark_date"] and not item["done"]
            ]
            if already_passed:
                first = already_passed[0]
                raise ValueError(
                    f"水线 {prev['watermark_date']} 内的分区被改动，账实已偏，水线拒绝推进："
                    f"{first['biz_date']} {first['reason']}"
                )

        done_upto_target = sum(
            1 for item in eligible if item["done"] and item["biz_date"] <= candidate
        )

        if prev:
            before_watermark = prev["watermark_date"]
            if candidate < prev["watermark_date"]:
                raise ValueError(
                    f"基准日 {as_of_text} 早于已落库水线 {prev['watermark_date']}，水线不回退"
                )
            # 历史水线同源复核：快照数字必须仍等于分区实物 + 裁决留痕。
            hist_cutoff = (
                _parse_day(prev["as_of_date"]) - timedelta(days=keep_days)
            ).isoformat()
            actual_prev = _audited_done_count(
                conn, domain_code, action, prev["watermark_date"], hist_cutoff
            )
            done_behind_prev = sum(
                1
                for item in eligible
                if item["done"] and item["biz_date"] <= prev["watermark_date"]
            )
            if actual_prev != int(prev["completed_count"]) or done_behind_prev != int(
                prev["completed_count"]
            ):
                raise ValueError(
                    "水位完成数与分区实物不同源："
                    f"快照记 {prev['completed_count']}，实物 {actual_prev}，已拦截"
                )
            if candidate == prev["watermark_date"]:
                noop = True
                completed_count = int(prev["completed_count"])
                after_watermark = prev["watermark_date"]
                message = (
                    f"水线已在 {prev['watermark_date']}，本次无新增完成，未重复落库"
                )
            else:
                newly_done = done_upto_target - done_behind_prev
                expected = int(prev["completed_count"]) + newly_done
                if done_upto_target != expected or newly_done <= 0:
                    raise ValueError(
                        "进度跳数已拦截："
                        f"水线 {prev['watermark_date']}→{candidate} 期望新增 "
                        f"{newly_done}，实物完成 {done_upto_target}，"
                        f"快照 {prev['completed_count']}"
                    )
                completed_count = done_upto_target
                after_watermark = candidate
                seq = int(prev["seq"]) + 1
        else:
            completed_count = done_upto_target
            after_watermark = candidate
            seq = 1

        if not noop:
            conn.execute(
                "INSERT INTO watermark_snapshots "
                "(domain_code, action_on_expire, policy_version, seq, "
                "watermark_date, completed_count, cutoff_date, as_of_date) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    domain_code,
                    action,
                    version,
                    seq,
                    candidate,
                    completed_count,
                    (as_of - timedelta(days=keep_days)).isoformat(),
                    as_of_text,
                ),
            )
            wrote_snapshot = True
            message = (
                f"水线推进至 {candidate}，完成 {completed_count} 个分区"
                f"（序号 {seq}）"
            )

        _log(
            conn,
            domain_code=domain_code,
            action=action,
            policy_version=version,
            as_of_date=as_of_text,
            ok=1,
            before_watermark=before_watermark,
            after_watermark=after_watermark,
            completed_count=completed_count,
            message=message,
        )
        # 卡片在提交前按同一事务快照生成：读不到不一致的中间态。
        card = build_card(conn, domain_code, as_of_text)
        conn.execute("COMMIT")
    except ValueError as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _log_failure(
            conn,
            domain_code=domain_code,
            action=action,
            policy_version=version,
            as_of_date=as_of_text,
            message=str(exc),
        )
        raise
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        text = str(exc)
        if "locked" in text.lower() or "busy" in text.lower():
            message = "水位正在被另一操作推进，请稍后重试"
        else:
            message = f"水位落库失败：{text}"
        _log_failure(
            conn,
            domain_code=domain_code,
            action=action,
            policy_version=version,
            as_of_date=as_of_text,
            message=message,
        )
        raise ValueError(message) from exc
    finally:
        conn.isolation_level = prev_isolation

    return {
        "ok": True,
        "advanced": wrote_snapshot,
        "message": message,
        "watermark_date": after_watermark,
        "completed_count": completed_count,
        "card": card,
    }


def _cards_by_domain(conn: sqlite3.Connection, as_of_date: str | None) -> dict[str, dict]:
    return {card["domain_code"]: card for card in list_cards(conn, as_of_date)}


def annotate_partitions(
    conn: sqlite3.Connection,
    items: list[dict],
    as_of_date: str | None = None,
) -> list[dict]:
    """给实物分区加上水线视角：水线内是否应裁未裁、水线外是否未齐。"""
    cards = _cards_by_domain(conn, as_of_date)
    for item in items:
        card = cards.get(item["domain_code"])
        if not card:
            item["progress_state"] = "无现行策略"
            item["progress_note"] = ""
            item["in_watermark"] = False
            item["should_be_done"] = False
            continue
        action = card["action_on_expire"]
        expired = item["biz_date"] <= card["cutoff_date"]
        done = expired and _physical_done(
            action,
            item["storage_state"],
            item["row_count"],
            item["archive_uri"],
        )
        watermark_date = card["watermark_date"]
        in_watermark = bool(watermark_date and item["biz_date"] <= watermark_date)
        item["in_watermark"] = in_watermark
        item["should_be_done"] = expired
        item["progress_action"] = action
        item["progress_watermark"] = watermark_date
        if not expired:
            item["progress_state"] = "未到期"
            item["progress_note"] = ""
        elif done and in_watermark:
            item["progress_state"] = "水线内已齐"
            item["progress_note"] = ""
        elif done:
            item["progress_state"] = "已裁且水线外"
            item["progress_note"] = "已按动作裁撤，等待水线推进"
        elif in_watermark:
            item["progress_state"] = "水线内应裁未裁"
            item["progress_note"] = (
                f"水线 {watermark_date} 已越过本分区，但实物仍是「"
                f"{item['storage_state']}」，账实已偏"
            )
        else:
            item["progress_state"] = "水线外未齐"
            item["progress_note"] = f"尚未{action}，阻塞水线推进"
    return items


def annotate_judgments(
    conn: sqlite3.Connection,
    items: list[dict],
    as_of_date: str | None = None,
) -> list[dict]:
    """给裁决队列加上水线视角：执行是否落在水线内、完成裁决与实物是否同源。"""
    cards = _cards_by_domain(conn, as_of_date)
    for item in items:
        card = cards.get(item["domain_code"])
        if not card:
            item["progress_state"] = "无现行策略"
            item["progress_note"] = ""
            item["in_watermark"] = False
            continue
        watermark_date = card["watermark_date"]
        in_watermark = bool(watermark_date and item["biz_date"] <= watermark_date)
        item["in_watermark"] = in_watermark
        part = conn.execute(
            "SELECT storage_state, row_count, archive_uri FROM partitions WHERE id = ?",
            (item["partition_id"],),
        ).fetchone()
        physical = (
            _physical_done(
                item["decided_action"],
                part["storage_state"],
                part["row_count"],
                part["archive_uri"],
            )
            if part
            else False
        )
        item["physical_done"] = physical
        if item["status"] == "已完成":
            if physical:
                item["progress_state"] = (
                    "水线内已完成" if in_watermark else "水线外已完成"
                )
                item["progress_note"] = ""
            else:
                item["progress_state"] = "完成但实物不符"
                item["progress_note"] = (
                    f"裁决已完成，分区实物是「{part['storage_state'] if part else '缺失'}」，"
                    "账实不符，水线推进将拦截"
                )
        elif item["status"] in ("待执行", "执行中", "已失败"):
            if in_watermark:
                item["progress_state"] = "水线内未执行"
                item["progress_note"] = (
                    f"水线 {watermark_date} 已越过本分区，裁决仍未完成，"
                    "水线推进将拦截"
                )
            else:
                item["progress_state"] = "水线外待执行"
                item["progress_note"] = ""
        else:
            item["progress_state"] = item["status"]
            item["progress_note"] = ""
    return items


def list_watermarks(conn: sqlite3.Connection, domain_code: str | None = None) -> list[dict]:
    sql = (
        "SELECT id, domain_code, action_on_expire, policy_version, seq, "
        "watermark_date, completed_count, cutoff_date, as_of_date, created_at "
        "FROM watermark_snapshots"
    )
    params: list[object] = []
    if domain_code:
        sql += " WHERE domain_code = ?"
        params.append(domain_code)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_advance_logs(
    conn: sqlite3.Connection, domain_code: str | None = None
) -> list[dict]:
    sql = (
        "SELECT id, domain_code, action_on_expire, policy_version, as_of_date, ok, "
        "before_watermark, after_watermark, completed_count, message, created_at "
        "FROM progress_advance_logs"
    )
    params: list[object] = []
    if domain_code:
        sql += " WHERE domain_code = ?"
        params.append(domain_code)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
