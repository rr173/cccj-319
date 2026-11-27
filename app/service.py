"""业务核心：工作分钟预算、跨团队日历、暂停、更正与升级。

约定：
- 所有时刻是整数 epoch 分钟（UTC）。
- 响应 = 受理 -> 首次有用答复；解决 = 受理 -> 解决，等待客户补资料期间暂停。
- 完成恰好用完预算不算超时（严格大于才算）。
- 原事实（original）一经判定不再改写；当前判定（current）随日历更正重算。
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import calendar as calmod
from . import timeutil


class Conflict(Exception):
    """请求与工单/升级当前状态冲突。"""

    def __init__(self, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(Exception):
    pass


# --------------------------------------------------------------------------
# 时钟
# --------------------------------------------------------------------------


def real_now_minute() -> int:
    return int(time.time() // 60)


def now_minute(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT at FROM clock_overrides ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["at"]) if row else real_now_minute()


def set_clock(conn: sqlite3.Connection, at: int, actor: str) -> int:
    """登记可控的当前时刻（值班员可回放历史或推进到未来）。

    历史事实仍以登记过的行为时刻为准；此处的“当前时刻”只影响未完成
    工单的已耗计算与判定，(原事实) 字段不会因此改写。
    """
    conn.execute(
        "INSERT INTO clock_overrides (at, real_at, created_by) VALUES (?, ?, ?)",
        (at, real_now_minute(), actor),
    )
    return at


def clock_info(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM clock_overrides ORDER BY id DESC LIMIT 1").fetchone()
    current = now_minute(conn)
    return {
        "now": timeutil.iso(current),
        "now_minute": current,
        "controlled": row is not None,
        "real_now": timeutil.iso(real_now_minute()),
    }


# --------------------------------------------------------------------------
# 团队
# --------------------------------------------------------------------------


def create_team(conn, name: str, response_budget_min: int, resolution_budget_min: int) -> dict:
    try:
        cur = conn.execute(
            "INSERT INTO teams (name, response_budget_min, resolution_budget_min) VALUES (?, ?, ?)",
            (name, response_budget_min, resolution_budget_min),
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict("团队名称已存在") from exc
    return get_team(conn, cur.lastrowid)


def get_team(conn, team_id: int) -> dict:
    row = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
    if row is None:
        raise NotFound(f"团队 {team_id} 不存在")
    return dict(row)


def update_team_budgets(conn, team_id: int, response_budget_min: int | None, resolution_budget_min: int | None) -> dict:
    get_team(conn, team_id)
    if response_budget_min is not None:
        conn.execute("UPDATE teams SET response_budget_min = ? WHERE id = ?", (response_budget_min, team_id))
    if resolution_budget_min is not None:
        conn.execute("UPDATE teams SET resolution_budget_min = ? WHERE id = ?", (resolution_budget_min, team_id))
    return get_team(conn, team_id)


# --------------------------------------------------------------------------
# 工单创建 / 行为登记
# --------------------------------------------------------------------------


def create_ticket(conn, title: str, team_id: int, now: int, actor: str,
                  response_budget_min: int | None = None,
                  resolution_budget_min: int | None = None,
                  accept: bool = True) -> int:
    """登记工单并冻结两个工作分钟预算。

    登记与受理是两个时刻：预算在登记时冻结，两个计时器都从受理起算。
    默认登记即受理；accept=False 时工单处于 new，等待 accept_ticket。
    """
    team = get_team(conn, team_id)
    rb = team["response_budget_min"] if response_budget_min is None else response_budget_min
    sb = team["resolution_budget_min"] if resolution_budget_min is None else resolution_budget_min
    cur = conn.execute(
        """INSERT INTO tickets
           (title, status, initial_team_id, current_team_id, created_at, accepted_at,
            response_budget_min, resolution_budget_min)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, "open" if accept else "new", team_id, team_id, now, now if accept else None, rb, sb),
    )
    ticket_id = int(cur.lastrowid)
    record_event(conn, ticket_id, "created", actor, now, {"team_id": team_id})
    if accept:
        record_event(conn, ticket_id, "accepted", actor, now, {})
        evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def accept_ticket(conn, ticket_id: int, actor: str, at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["accepted_at"] is not None:
        raise Conflict("工单已受理")
    if t["status"] == "resolved":
        raise Conflict("工单已解决")
    at = now if at is None else at
    if at > now:
        raise Conflict("受理时刻不能晚于当前时刻")
    if at < t["created_at"]:
        raise Conflict("受理时刻不能早于登记时刻")
    conn.execute(
        "UPDATE tickets SET accepted_at = ?, status = 'open' WHERE id = ?", (at, ticket_id)
    )
    record_event(conn, ticket_id, "accepted", actor, at, {})
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def _ticket_row(conn, ticket_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise NotFound(f"工单 {ticket_id} 不存在")
    return row


def _validate_event_time(t, at: int, now: int, conn) -> None:
    if at > now:
        raise Conflict("行为时刻不能晚于当前时刻", detail={"now": timeutil.iso(now)})
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    if at < t["accepted_at"]:
        raise Conflict("行为时刻不能早于受理时刻")


def record_event(conn, ticket_id: int, kind: str, actor: str, at: int, payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO ticket_events (ticket_id, kind, actor, payload_json, at) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, kind, actor, json.dumps(payload or {}, ensure_ascii=False), at),
    )
    return int(cur.lastrowid)


def list_events(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "actor": r["actor"],
            "at": timeutil.iso(r["at"]),
            "payload": json.loads(r["payload_json"]),
        }
        for r in rows
    ]


def add_note(conn, ticket_id: int, actor: str, content: str, at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理，不能登记行为")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    record_event(conn, ticket_id, "note", actor, at, {"content": content})
    return ticket_id


def add_agent_reply(conn, ticket_id: int, actor: str, useful: bool, content: str | None,
                    at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["status"] == "resolved":
        raise Conflict("工单已解决，不能再登记答复")
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    record_event(conn, ticket_id, "agent_reply" if useful else "agent_note", actor, at,
                 {"useful": useful, "content": content})
    if useful:
        # 首次有用答复取最早的一条（允许补录更早的答复）
        if t["first_reply_at"] is None or at < t["first_reply_at"]:
            conn.execute("UPDATE tickets SET first_reply_at = ? WHERE id = ?", (at, ticket_id))
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def add_customer_reply(conn, ticket_id: int, actor: str, content: str | None,
                       at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["status"] == "resolved":
        raise Conflict("工单已解决，不能再登记客户回复")
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    event_id = record_event(conn, ticket_id, "customer_reply", actor, at, {"content": content})
    # 只有“关联的客户回复”会结束等待；普通留言（agent note / 系统消息）不会。
    wait = conn.execute(
        "SELECT * FROM customer_waits WHERE ticket_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    if wait is not None:
        if at < wait["requested_at"]:
            raise Conflict(
                "结束等待的客户回复不能早于本次等待的发起时刻",
                detail={"wait_requested_at": timeutil.iso(wait["requested_at"])},
            )
        conn.execute(
            "UPDATE customer_waits SET ended_at = ?, ended_by_event = ? WHERE id = ?",
            (at, event_id, wait["id"]),
        )
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def request_customer_info(conn, ticket_id: int, actor: str, reason: str | None,
                          at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["status"] == "resolved":
        raise Conflict("工单已解决，不能发起等待")
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    if actor in ("customer", "client"):
        raise Conflict("等待只能由客服发起")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    open_wait = conn.execute(
        "SELECT id FROM customer_waits WHERE ticket_id = ? AND ended_at IS NULL LIMIT 1", (ticket_id,)
    ).fetchone()
    if open_wait is not None:
        raise Conflict("工单已处于等待客户补资料状态")
    cur = conn.execute(
        "INSERT INTO customer_waits (ticket_id, requested_at, requested_by) VALUES (?, ?, ?)",
        (ticket_id, at, actor),
    )
    record_event(conn, ticket_id, "wait_requested", actor, at, {"reason": reason, "wait_id": cur.lastrowid})
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def resolve_ticket(conn, ticket_id: int, actor: str, at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["status"] == "resolved":
        raise Conflict("工单已解决")
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    conn.execute("UPDATE tickets SET status = 'resolved', resolved_at = ? WHERE id = ?", (at, ticket_id))
    # 结案时关闭仍在进行的等待（客户始终没回，但工单已结束）。
    conn.execute(
        "UPDATE customer_waits SET ended_at = ? WHERE ticket_id = ? AND ended_at IS NULL",
        (at, ticket_id),
    )
    record_event(conn, ticket_id, "resolved", actor, at, {})
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


def transfer_ticket(conn, ticket_id: int, to_team_id: int, actor: str, at: int | None, now: int) -> int:
    t = _ticket_row(conn, ticket_id)
    if t["status"] == "resolved":
        raise Conflict("工单已解决，不能转组")
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理")
    get_team(conn, to_team_id)
    if to_team_id == t["current_team_id"]:
        raise Conflict("工单已在该团队")
    at = now if at is None else at
    _validate_event_time(t, at, now, conn)
    last = conn.execute(
        "SELECT MAX(at) AS m FROM ticket_transfers WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()["m"]
    if last is not None and at < last:
        raise Conflict(
            "转组时刻不能早于上一次转组",
            detail={"last_transfer_at": timeutil.iso(last)},
        )
    conn.execute(
        "INSERT INTO ticket_transfers (ticket_id, from_team_id, to_team_id, at) VALUES (?, ?, ?, ?)",
        (ticket_id, t["current_team_id"], to_team_id, at),
    )
    conn.execute("UPDATE tickets SET current_team_id = ? WHERE id = ?", (to_team_id, ticket_id))
    record_event(conn, ticket_id, "transferred", actor, at,
                 {"from_team_id": t["current_team_id"], "to_team_id": to_team_id})
    evaluate_ticket(conn, ticket_id, now)
    return ticket_id


# --------------------------------------------------------------------------
# 日历
# --------------------------------------------------------------------------


def set_team_calendar(conn, team_id: int, day: str, raw_intervals, actor: str, now: int) -> dict:
    get_team(conn, team_id)
    try:
        day_start = timeutil.parse_day(day)
    except ValueError as exc:
        raise Conflict("day 必须是 YYYY-MM-DD") from exc
    if day != timeutil.day_of(day_start):
        raise Conflict("day 必须是 YYYY-MM-DD")
    if day_start > now:
        raise Conflict("只能更正已过（含当前）时段的工作日历")
    try:
        intervals = calmod.validate_intervals(raw_intervals)
    except ValueError as exc:
        raise Conflict(str(exc)) from exc

    before = calmod.day_intervals(conn, team_id, day)
    calmod.replace_day(conn, team_id, day, intervals, actor, now)
    after = calmod.day_intervals(conn, team_id, day)
    cur = conn.execute(
        "INSERT INTO calendar_corrections (team_id, day, before_json, after_json, actor, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (team_id, day, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), actor, now),
    )
    correction_id = int(cur.lastrowid)

    affected = _tickets_overlapping(conn, day_start, day_start + timeutil.MINUTES_PER_DAY, now)
    results = []
    for ticket_id in affected:
        evaluate_ticket(conn, ticket_id, now)
        results.append(ticket_id)
    return {
        "correction_id": correction_id,
        "team_id": team_id,
        "day": day,
        "before": before,
        "after": after,
        "affected_ticket_ids": results,
    }


def _tickets_overlapping(conn, window_start: int, window_end: int, now: int) -> list[int]:
    rows = conn.execute("SELECT id, accepted_at, resolved_at FROM tickets").fetchall()
    out = []
    for r in rows:
        if r["accepted_at"] is None:
            continue
        end = r["resolved_at"] if r["resolved_at"] is not None else now
        if r["accepted_at"] < window_end and end >= window_start:
            out.append(int(r["id"]))
    return out


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------


def _segments(t, transfers: list[sqlite3.Row], start: int, end: int) -> list[tuple[int, int, int]]:
    """按转组把 [start, end) 切成 (起, 止, 负责团队)。转组不归零，各段累加。"""
    segs: list[tuple[int, int, int]] = []
    team = int(t["initial_team_id"])
    pos = start
    for tr in transfers:
        tr_at = int(tr["at"])
        if tr_at <= start:
            team = int(tr["to_team_id"])
            continue
        if tr_at >= end:
            break
        if tr_at > pos:
            segs.append((pos, tr_at, team))
        pos = tr_at
        team = int(tr["to_team_id"])
    if end > pos:
        segs.append((pos, end, team))
    return segs


def _elapsed_minutes(conn, t, transfers, waits, start: int, end: int, pause: bool) -> tuple[int, int]:
    """返回 (计入的工作分钟, 因暂停扣掉的工作分钟)。"""
    if end <= start:
        return 0, 0
    segs = _segments(t, transfers, start, end)
    teams = {team for _, _, team in segs}
    cal = calmod.Calendar.load(conn, teams, start, end)

    pauses: list[tuple[int, int]] = []
    if pause:
        for w in waits:
            ws = max(int(w["requested_at"]), start)
            we = int(w["ended_at"]) if w["ended_at"] is not None else end
            we = min(we, end)
            if we > ws:
                pauses.append((ws, we))

    gross = 0
    paused = 0
    for s, e, team in segs:
        gross += cal.minutes(team, s, e)
        for ps, pe in pauses:
            a, b = max(s, ps), min(e, pe)
            if b > a:
                paused += cal.minutes(team, a, b)
    return max(gross - paused, 0), max(paused, 0)


def _apply_dimension(conn, t, key: str, elapsed: int, concluded: bool, now: int) -> None:
    budget = int(t[f"{key}_budget_min"])
    current = 1 if elapsed > budget else 0
    prev = int(t[f"{key}_current_breach"])
    orig_flag = t[f"{key}_original_breach"]

    sets: dict[str, int] = {}
    if orig_flag is None and (concluded or current == 1):
        # 原事实：在“结案”或“第一次超时”时定格，此后不再改写。
        sets[f"{key}_original_breach"] = current
        sets[f"{key}_original_elapsed"] = elapsed
        sets[f"{key}_original_judged_at"] = now

    if current != prev:
        sets[f"{key}_current_breach"] = current
        sets[f"{key}_current_judged_at"] = now
    elif t[f"{key}_current_judged_at"] is None:
        sets[f"{key}_current_judged_at"] = now

    if prev == 0 and current == 1:
        # 新一波超时：旧升级（若有）不会复活，另开一条可签收记录。
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM escalations WHERE ticket_id = ? AND dimension = ?",
            (t["id"], key),
        ).fetchone()
        seq = int(row["m"]) + 1
        conn.execute(
            """INSERT INTO escalations
               (ticket_id, team_id, dimension, seq, opened_at, elapsed_min, budget_min, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (t["id"], t["current_team_id"], key, seq, now, elapsed, budget),
        )
        sets[f"{key}_breach_episode"] = seq
    elif prev == 1 and current == 0:
        # 更正后原超时变为未超时：未签收的取消；已签收的不可删除，结案保留「判断已更正」。
        rows = conn.execute(
            "SELECT id, status FROM escalations WHERE ticket_id = ? AND dimension = ?"
            " AND status IN ('open', 'acknowledged')",
            (t["id"], key),
        ).fetchall()
        for esc in rows:
            if esc["status"] == "open":
                conn.execute(
                    "UPDATE escalations SET status = 'cancelled', cancelled_at = ?,"
                    " cancel_reason = 'judgment_corrected' WHERE id = ?",
                    (now, esc["id"]),
                )
            else:
                conn.execute(
                    "UPDATE escalations SET status = 'closed', closed_at = ?,"
                    " closure_note = '判断已更正' WHERE id = ?",
                    (now, esc["id"]),
                )

    if sets:
        cols = ", ".join(f"{c} = ?" for c in sets)
        conn.execute(f"UPDATE tickets SET {cols} WHERE id = ?", (*sets.values(), t["id"]))
        for c, v in sets.items():
            t[c] = v
    t[f"{key}_current_breach"] = current


def evaluate_ticket(conn, ticket_id: int, now: int) -> dict:
    t = dict(_ticket_row(conn, ticket_id))
    if t["accepted_at"] is None:
        return t
    now = max(now, int(t["accepted_at"]))
    transfers = conn.execute(
        "SELECT * FROM ticket_transfers WHERE ticket_id = ? ORDER BY at, id", (ticket_id,)
    ).fetchall()
    waits = conn.execute(
        "SELECT * FROM customer_waits WHERE ticket_id = ? ORDER BY requested_at, id", (ticket_id,)
    ).fetchall()

    # 响应计时到首次有用答复为止；若一直没答复，结案时定格（视为承诺落空）。
    response_end = t["first_reply_at"]
    if response_end is None:
        response_end = t["resolved_at"] if t["resolved_at"] is not None else now
    response_concluded = t["first_reply_at"] is not None or t["resolved_at"] is not None

    resolution_end = t["resolved_at"] if t["resolved_at"] is not None else now
    resolution_concluded = t["resolved_at"] is not None

    resp_elapsed, _ = _elapsed_minutes(conn, t, transfers, waits, t["accepted_at"], response_end, pause=False)
    res_elapsed, _ = _elapsed_minutes(conn, t, transfers, waits, t["accepted_at"], resolution_end, pause=True)

    _apply_dimension(conn, t, "response", resp_elapsed, response_concluded, now)
    _apply_dimension(conn, t, "resolution", res_elapsed, resolution_concluded, now)
    return t


# --------------------------------------------------------------------------
# 升级签收
# --------------------------------------------------------------------------


def _escalation_dict(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "team_id": row["team_id"],
        "dimension": row["dimension"],
        "seq": row["seq"],
        "status": row["status"],
        "opened_at": timeutil.iso(row["opened_at"]),
        "elapsed_min": row["elapsed_min"],
        "budget_min": row["budget_min"],
        "acknowledged_at": timeutil.iso(row["acknowledged_at"]),
        "acknowledged_by": row["acknowledged_by"],
        "ack_note": row["ack_note"],
        "cancelled_at": timeutil.iso(row["cancelled_at"]),
        "cancel_reason": row["cancel_reason"],
        "closed_at": timeutil.iso(row["closed_at"]),
        "closure_note": row["closure_note"],
    }


def get_escalation(conn, escalation_id: int) -> dict:
    row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    if row is None:
        raise NotFound(f"升级 {escalation_id} 不存在")
    return _escalation_dict(row)


def list_escalations(conn, ticket_id: int | None = None, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM escalations"
    clauses, args = [], []
    if ticket_id is not None:
        clauses.append("ticket_id = ?")
        args.append(ticket_id)
    if status is not None:
        clauses.append("status = ?")
        args.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ticket_id, dimension, seq"
    return [_escalation_dict(r) for r in conn.execute(sql, args).fetchall()]


def acknowledge_escalation(conn, escalation_id: int, actor: str, note: str | None, now: int) -> dict:
    """签收。必须在 BEGIN IMMEDIATE 事务内调用，保证看不见已被并发撤销的升级。"""
    row = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    if row is None:
        raise NotFound(f"升级 {escalation_id} 不存在")

    if row["status"] == "acknowledged":
        if row["acknowledged_by"] == actor:
            return _escalation_dict(row)
        raise Conflict(
            "升级已被他人签收",
            detail={"escalation": _escalation_dict(row)},
        )
    if row["status"] in ("cancelled", "closed"):
        raise Conflict(
            "升级已被撤销，不能签收",
            detail={"escalation": _escalation_dict(row)},
        )
    if row["status"] != "open":
        raise Conflict("升级当前状态不可签收", detail={"escalation": _escalation_dict(row)})

    conn.execute(
        "UPDATE escalations SET status = 'acknowledged', acknowledged_at = ?, acknowledged_by = ?,"
        " ack_note = ? WHERE id = ? AND status = 'open'",
        (now, actor, note, escalation_id),
    )
    refreshed = conn.execute("SELECT * FROM escalations WHERE id = ?", (escalation_id,)).fetchone()
    return _escalation_dict(refreshed)


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def _dimension_view(t, key: str, elapsed: int, concluded: bool, paused: int) -> dict:
    budget = int(t[f"{key}_budget_min"])
    result = {
        "budget_min": budget,
        "elapsed_min": elapsed,
        "remaining_min": max(budget - elapsed, 0),
        "over_by_min": max(elapsed - budget, 0),
        "completed": (
            t["first_reply_at"] is not None if key == "response" else t["resolved_at"] is not None
        ),
        "completed_at": timeutil.iso(t["first_reply_at"] if key == "response" else t["resolved_at"]),
        "concluded": concluded,
        "started_at": timeutil.iso(t["accepted_at"]),
        # 原事实：第一次判定时定格，改动日历不改写
        "original": {
            "breach": None if t[f"{key}_original_breach"] is None else bool(t[f"{key}_original_breach"]),
            "elapsed_min": t[f"{key}_original_elapsed"],
            "judged_at": timeutil.iso(t[f"{key}_original_judged_at"]),
        },
        # 当前判定：以现有日历/当前时刻重算
        "current": {
            "breach": bool(t[f"{key}_current_breach"]),
            "judged_at": timeutil.iso(t[f"{key}_current_judged_at"]),
            "episode": int(t[f"{key}_breach_episode"]),
        },
    }
    if key == "resolution":
        result["paused_min"] = paused
        result["waiting"] = bool(t["waiting"])
        result["wait_started_at"] = timeutil.iso(t["wait_started_at"])
    return result


def ticket_detail(conn, ticket_id: int) -> dict:
    t = dict(_ticket_row(conn, ticket_id))
    t["evaluated_now"] = t["resolved_at"] if t["resolved_at"] is not None else now_minute(conn)
    return _ticket_detail_from_row(conn, t)


def _ticket_detail_from_row(conn, t: dict) -> dict:
    ticket_id = t["id"]
    now = t["evaluated_now"]
    if t["accepted_at"] is not None:
        now = max(now, int(t["accepted_at"]))
    transfers = conn.execute(
        "SELECT * FROM ticket_transfers WHERE ticket_id = ? ORDER BY at, id", (ticket_id,)
    ).fetchall()
    waits = conn.execute(
        "SELECT * FROM customer_waits WHERE ticket_id = ? ORDER BY requested_at, id", (ticket_id,)
    ).fetchall()

    response_end = t["first_reply_at"]
    if response_end is None:
        response_end = t["resolved_at"] if t["resolved_at"] is not None else now
    response_concluded = t["first_reply_at"] is not None or t["resolved_at"] is not None
    resolution_end = t["resolved_at"] if t["resolved_at"] is not None else now
    resolution_concluded = t["resolved_at"] is not None

    if t["accepted_at"] is None:
        resp_elapsed = res_elapsed = res_paused = res_gross = 0
    else:
        resp_elapsed, _ = _elapsed_minutes(
            conn, t, transfers, waits, t["accepted_at"], response_end, pause=False
        )
        res_elapsed, res_paused = _elapsed_minutes(
            conn, t, transfers, waits, t["accepted_at"], resolution_end, pause=True
        )
        res_gross, _ = _elapsed_minutes(
            conn, t, transfers, waits, t["accepted_at"], resolution_end, pause=False
        )

    open_wait = next((w for w in waits if w["ended_at"] is None), None)
    t["waiting"] = open_wait is not None
    t["wait_started_at"] = open_wait["requested_at"] if open_wait else None

    escalations = [
        _escalation_dict(r)
        for r in conn.execute(
            "SELECT * FROM escalations WHERE ticket_id = ? ORDER BY dimension, seq", (ticket_id,)
        ).fetchall()
    ]

    def latest(dim: str) -> dict | None:
        items = [e for e in escalations if e["dimension"] == dim]
        return items[-1] if items else None

    return {
        "id": t["id"],
        "title": t["title"],
        "status": t["status"],
        "team_id": t["current_team_id"],
        "initial_team_id": t["initial_team_id"],
        "created_at": timeutil.iso(t["created_at"]),
        "accepted_at": timeutil.iso(t["accepted_at"]),
        "resolved_at": timeutil.iso(t["resolved_at"]),
        "response": {
            **_dimension_view(t, "response", resp_elapsed, response_concluded, 0),
            "escalation": latest("response"),
        },
        "resolution": {
            **_dimension_view(t, "resolution", res_elapsed, resolution_concluded, res_paused),
            "gross_elapsed_min": res_gross,
            "escalation": latest("resolution"),
        },
        "escalations": escalations,
        "transfers": [
            {"from_team_id": r["from_team_id"], "to_team_id": r["to_team_id"], "at": timeutil.iso(r["at"])}
            for r in transfers
        ],
        "waits": [
            {
                "id": r["id"],
                "requested_at": timeutil.iso(r["requested_at"]),
                "requested_by": r["requested_by"],
                "ended_at": timeutil.iso(r["ended_at"]),
                "active": r["ended_at"] is None,
            }
            for r in waits
        ],
    }


def ticket_detail_at(conn, ticket_id: int, now: int) -> dict:
    t = dict(_ticket_row(conn, ticket_id))
    t["evaluated_now"] = t["resolved_at"] if t["resolved_at"] is not None else now
    return _ticket_detail_from_row(conn, t)


def list_tickets(conn, status: str | None = None, team_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM tickets"
    clauses, args = [], []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if team_id is not None:
        clauses.append("current_team_id = ?")
        args.append(team_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    now = now_minute(conn)
    rows = conn.execute(sql, args).fetchall()
    return [ticket_detail_at(conn, int(r["id"]), now) for r in rows]


def day_calendar(conn, team_id: int, day: str) -> list[dict]:
    from . import calendar as _cal

    get_team(conn, team_id)
    try:
        timeutil.parse_day(day)
    except ValueError as exc:
        raise Conflict("day 必须是 YYYY-MM-DD") from exc
    return _cal.day_intervals(conn, team_id, day)


def reevaluate_open_tickets(conn, now: int) -> list[int]:
    rows = conn.execute("SELECT id FROM tickets WHERE status = 'open' ORDER BY id").fetchall()
    ids = []
    for r in rows:
        evaluate_ticket(conn, int(r["id"]), now)
        ids.append(int(r["id"]))
    return ids
