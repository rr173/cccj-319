"""回拨预约：坐席档案、可用时段、预约/改期/取消/开始/结束与全局可行性。

口径与不变量：
- 一切时刻为整数 epoch 分钟（UTC），窗口为半开区间 [start, end)。
- 回拨业务时刻 = max(可控时钟, 回拨高水位)；时钟回放只能影响原 SLA 口径，
  不能复活已错过的预约（错过判定一旦到达即不可逆）。
- 每张已受理、未结案工单同时最多一笔 pending/in_progress 回拨。
- 可执行坐席须：属于工单当前负责组、具备所需技能、可用时段覆盖整个窗口，
  且同一坐席的有效预约互不重叠；一次回拨不能分段换人。
- 任何档案/预约调整都要验证“调整后所有尚有效预约可同时兑现”，允许在
  pending 预约间重新分配坐席；不可行则整体拒绝，原承诺不变。
"""

from __future__ import annotations

import sqlite3

from . import service
from . import timeutil

# 有效（仍占排班容量）的状态
ACTIVE_STATUSES = ("pending", "in_progress")
TERMINAL_STATUSES = ("completed", "cancelled", "missed")


class Conflict(service.Conflict):
    pass


class NotFound(service.NotFound):
    pass


# --------------------------------------------------------------------------
# 回拨业务时刻（高水位，单调不回落）
# --------------------------------------------------------------------------


def business_now(conn: sqlite3.Connection) -> int:
    """回拨业务当前时刻：可控时钟与回拨高水位的较大者。"""
    controlled = service.now_minute(conn)
    row = conn.execute("SELECT high_water_minute FROM callback_clock WHERE id = 1").fetchone()
    hwm = int(row["high_water_minute"]) if row else controlled
    return max(controlled, hwm)


def _bump_clock(conn: sqlite3.Connection, now: int) -> None:
    """把回拨高水位推进到 now（只增不减）。"""
    conn.execute(
        "INSERT INTO callback_clock (id, high_water_minute) VALUES (1, ?)"
        " ON CONFLICT(id) DO UPDATE SET high_water_minute = MAX(high_water_minute, excluded.high_water_minute)",
        (now,),
    )


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------


def parse_minute(value) -> int:
    """接受整数分钟或 ISO-8601 字符串。"""
    if isinstance(value, bool):
        raise Conflict("时刻必须是整数分钟或 ISO-8601 时间")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return timeutil.parse_iso(value)
        except ValueError as exc:
            raise Conflict("时间格式应为整数分钟或 ISO-8601，例如 2026-01-01T09:00:00Z") from exc
    raise Conflict("时刻必须是整数分钟或 ISO-8601 时间")


def validate_window(start: int, end: int) -> None:
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise Conflict("窗口端点必须是整数分钟")
    if start >= end:
        raise Conflict("窗口须为半开区间且 start < end")


def validate_intervals(raw: list) -> list[tuple[int, int]]:
    """校验绝对分钟半开区间列表，互不重叠。"""
    if not isinstance(raw, list) or not raw:
        raise Conflict("intervals 必须是非空列表")
    intervals: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, dict):
            s = item.get("start_minute", item.get("start"))
            e = item.get("end_minute", item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            s, e = item
        else:
            raise Conflict("每个时段必须是 [start, end] 或 {start_minute, end_minute}")
        s, e = parse_minute(s), parse_minute(e)
        if s >= e:
            raise Conflict("时段须为半开区间且 start < end")
        intervals.append((s, e))
    intervals.sort()
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        if s2 < e1:
            raise Conflict("同一坐席的可用时段不能重叠")
    return intervals


def validate_skills(raw: list) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise Conflict("skills 必须是非空字符串列表")
    skills: list[str] = []
    for s in raw:
        if not isinstance(s, str) or not s.strip():
            raise Conflict("技能名称必须是非空字符串")
        skills.append(s.strip())
    if len(set(skills)) != len(skills):
        raise Conflict("技能不能重复")
    return skills


# --------------------------------------------------------------------------
# 坐席档案（主管登记：团队、技能、可用时段）
# --------------------------------------------------------------------------


def _agent_row(conn, agent_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        raise NotFound(f"坐席 {agent_id} 不存在")
    return row


def agent_skills(conn, agent_id: int) -> list[str]:
    rows = conn.execute("SELECT skill FROM agent_skills WHERE agent_id = ? ORDER BY skill", (agent_id,)).fetchall()
    return [r["skill"] for r in rows]


def agent_availability(conn, agent_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, start_minute, end_minute FROM agent_availability WHERE agent_id = ?"
        " ORDER BY start_minute, id",
        (agent_id,),
    ).fetchall()
    return [
        {"id": r["id"], "start_minute": r["start_minute"], "end_minute": r["end_minute"],
         "start": timeutil.iso(r["start_minute"]), "end": timeutil.iso(r["end_minute"])}
        for r in rows
    ]


def agent_detail(conn, agent_id: int) -> dict:
    row = _agent_row(conn, agent_id)
    return {
        "id": row["id"],
        "name": row["name"],
        "team_id": row["team_id"],
        "skills": agent_skills(conn, agent_id),
        "availability": agent_availability(conn, agent_id),
    }


def list_agents(conn, team_id: int | None = None) -> list[dict]:
    sql = "SELECT id FROM agents"
    args: list = []
    if team_id is not None:
        sql += " WHERE team_id = ?"
        args.append(team_id)
    sql += " ORDER BY id"
    return [agent_detail(conn, int(r["id"])) for r in conn.execute(sql, args).fetchall()]


def create_agent(conn, name: str, team_id: int, skills: list, availability: list, now: int) -> dict:
    if not isinstance(name, str) or not name.strip():
        raise Conflict("坐席名称不能为空")
    service.get_team(conn, team_id)  # 团队必须存在
    skills = validate_skills(skills)
    intervals = validate_intervals(availability)
    cur = conn.execute(
        "INSERT INTO agents (name, team_id, created_at) VALUES (?, ?, ?)",
        (name.strip(), team_id, now),
    )
    agent_id = int(cur.lastrowid)
    _replace_skills(conn, agent_id, skills)
    _replace_availability(conn, agent_id, intervals)
    return agent_detail(conn, agent_id)


def _replace_skills(conn, agent_id: int, skills: list[str]) -> None:
    conn.execute("DELETE FROM agent_skills WHERE agent_id = ?", (agent_id,))
    conn.executemany(
        "INSERT INTO agent_skills (agent_id, skill) VALUES (?, ?)",
        [(agent_id, s) for s in skills],
    )


def _replace_availability(conn, agent_id: int, intervals: list[tuple[int, int]]) -> None:
    conn.execute("DELETE FROM agent_availability WHERE agent_id = ?", (agent_id,))
    conn.executemany(
        "INSERT INTO agent_availability (agent_id, start_minute, end_minute) VALUES (?, ?, ?)",
        [(agent_id, s, e) for s, e in intervals],
    )


def update_agent(conn, agent_id: int, name: str | None, team_id: int | None,
                 skills: list | None, availability: list | None, now: int) -> dict:
    """调整坐席档案。调整后所有尚有效预约仍须可同时兑现，否则整体拒绝。

    不可行时由外层事务回滚，档案与全部预约分配都保持原状（无部分更新）。
    可行时把求解器对 pending 预约的重新分配一并落库。
    """
    _agent_row(conn, agent_id)
    new_team = team_id
    if new_team is not None:
        service.get_team(conn, new_team)
    new_skills = validate_skills(skills) if skills is not None else None
    new_intervals = validate_intervals(availability) if availability is not None else None
    if name is not None and not name.strip():
        raise Conflict("坐席名称不能为空")

    # 先落候选变更（仍在外层 BEGIN IMMEDIATE 事务内），整体可行性不通过则回滚
    if name is not None:
        conn.execute("UPDATE agents SET name = ? WHERE id = ?", (name.strip(), agent_id))
    if new_team is not None:
        conn.execute("UPDATE agents SET team_id = ? WHERE id = ?", (new_team, agent_id))
    if new_skills is not None:
        _replace_skills(conn, agent_id, new_skills)
    if new_intervals is not None:
        _replace_availability(conn, agent_id, new_intervals)

    plan = assert_all_feasible(conn, now)
    _persist_reassignments(conn, plan)
    return agent_detail(conn, agent_id)


# --------------------------------------------------------------------------
# 可行性模型
# --------------------------------------------------------------------------


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    return s1 < e2 and s2 < e1


def _covers(avail: list[tuple[int, int]], start: int, end: int) -> bool:
    for s, e in avail:
        if s <= start and end <= e:
            return True
    return False


def _load_agent_model(conn) -> dict[int, dict]:
    model: dict[int, dict] = {}
    for a in conn.execute("SELECT * FROM agents").fetchall():
        aid = int(a["id"])
        skills = {r["skill"] for r in conn.execute(
            "SELECT skill FROM agent_skills WHERE agent_id = ?", (aid,)).fetchall()}
        avail = _merge([
            (int(r["start_minute"]), int(r["end_minute"]))
            for r in conn.execute(
                "SELECT start_minute, end_minute FROM agent_availability WHERE agent_id = ?", (aid,))
        ])
        model[aid] = {"team_id": int(a["team_id"]), "skills": skills, "availability": avail}
    return model


def _active_rows(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM callbacks WHERE status IN ('pending', 'in_progress') ORDER BY id"
    ).fetchall()


def _pin_feasible(row, agents: dict[int, dict]) -> tuple[bool, str]:
    """进行中的回拨：执行坐席固定，必须仍覆盖整个原窗口。"""
    aid = row["agent_id"]
    if aid is None:
        return False, "进行中的回拨缺少执行坐席"
    agent = agents.get(int(aid))
    if agent is None:
        return False, "执行坐席已不存在"
    if agent["team_id"] != int(row["team_id"]):
        return False, "执行坐席已不在承接团队"
    if row["skill"] not in agent["skills"]:
        return False, "执行坐席不再具备所需技能"
    if not _covers(agent["availability"], int(row["start_minute"]), int(row["end_minute"])):
        return False, "执行坐席的可用时段不再覆盖原窗口"
    return True, ""


def feasibility_plan(conn, now: int, *, extra: dict | None = None,
                     team_override: dict[int, int] | None = None,
                     exclude_ids: set[int] | None = None) -> dict[int, int]:
    """求一组让全部尚有效预约同时兑现的坐席分配。

    - in_progress 预约钉在执行坐席上（固定、不可换人）。
    - pending 预约可在合格坐席间重新分配（尚未向客户承诺具体坐席）。
    - extra：一条未落库的候选预约（id 用负数），用于新约/改期预判。
    - team_override：{callback_id: team_id}，仅用于 pending 预约的承接组预判
      （转组时目标组尚未在 callbacks.team_id 上落库）。
    返回 {callback_id: agent_id}；无解时抛 Conflict。
    """
    team_override = team_override or {}
    exclude_ids = exclude_ids or set()
    agents = _load_agent_model(conn)
    rows = [r for r in _active_rows(conn) if int(r["id"]) not in exclude_ids]
    if extra is not None:
        rows.append(extra)

    busy: dict[int, list[tuple[int, int]]] = {aid: [] for aid in agents}
    pending: list[sqlite3.Row | dict] = []

    for row in rows:
        s, e = int(row["start_minute"]), int(row["end_minute"])
        if row["status"] == "in_progress":
            ok, reason = _pin_feasible(row, agents)
            if not ok:
                raise Conflict(f"调整后存在无法兑现的回拨：{reason}")
            aid = int(row["agent_id"])
            for bs, be in busy[aid]:
                if _overlaps(s, e, bs, be):
                    raise Conflict("调整后同一坐席的回拨时间重叠")
            busy[aid].append((s, e))
        else:
            pending.append(row)

    def eligible(row) -> list[int]:
        s, e = int(row["start_minute"]), int(row["end_minute"])
        required_team = int(team_override.get(int(row["id"]), row["team_id"]))
        out = []
        for aid, agent in agents.items():
            if agent["team_id"] != required_team:
                continue
            if row["skill"] not in agent["skills"]:
                continue
            if not _covers(agent["availability"], s, e):
                continue
            if any(_overlaps(s, e, bs, be) for bs, be in busy[aid]):
                continue
            out.append(aid)
        return out

    # 约束最少的 pending 先分配（MRV），同序按开始时刻与 id，保证确定性
    pending.sort(key=lambda r: (len(eligible(r)), int(r["start_minute"]), int(r["id"])))
    assignment: dict[int, int] = {
        int(r["id"]): int(r["agent_id"])
        for r in rows if r["status"] == "in_progress"
    }

    def search(index: int) -> bool:
        if index == len(pending):
            return True
        row = pending[index]
        cid = int(row["id"])
        s, e = int(row["start_minute"]), int(row["end_minute"])
        for aid in eligible(row):
            if any(_overlaps(s, e, bs, be) for bs, be in busy[aid]):
                continue
            busy[aid].append((s, e))
            assignment[cid] = aid
            if search(index + 1):
                return True
            busy[aid].pop()
            del assignment[cid]
        return False

    if not search(0):
        raise Conflict("调整后存在无法安排的回拨预约")
    return assignment


def assert_all_feasible(conn, now: int) -> dict[int, int]:
    return feasibility_plan(conn, now)


# --------------------------------------------------------------------------
# 错过（missed）扫描：未开始且窗口终点已到 => 释放容量，不可逆
# --------------------------------------------------------------------------


def sweep_missed(conn, now: int) -> list[int]:
    """把窗口终点 <= 业务当前时刻、仍未开始的预约标记为 missed。

    高水位单调，时钟回放不会让已错过的预约复活。
    """
    ids: list[int] = []
    rows = conn.execute(
        "SELECT * FROM callbacks WHERE status = 'pending' AND end_minute <= ? ORDER BY id",
        (now,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE callbacks SET status = 'missed', missed_at = ? WHERE id = ? AND status = 'pending'",
            (now, int(row["id"])),
        )
        service.record_event(
            conn, int(row["ticket_id"]), "callback_missed", "system", now,
            {"callback_id": int(row["id"]), "window": [int(row["start_minute"]), int(row["end_minute"])]},
        )
        ids.append(int(row["id"]))
    return ids


# --------------------------------------------------------------------------
# 预约查询
# --------------------------------------------------------------------------


def _callback_dict(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "team_id": row["team_id"],
        "skill": row["skill"],
        "start_minute": row["start_minute"],
        "end_minute": row["end_minute"],
        "window_start": timeutil.iso(row["start_minute"]),
        "window_end": timeutil.iso(row["end_minute"]),
        "status": row["status"],
        "agent_id": row["agent_id"],
        "booked_at": timeutil.iso(row["booked_at"]),
        "reschedule_count": row["reschedule_count"],
        "started_at": timeutil.iso(row["started_at"]),
        "ended_at": timeutil.iso(row["ended_at"]),
        "cancelled_at": timeutil.iso(row["cancelled_at"]),
        "cancel_reason": row["cancel_reason"],
        "missed_at": timeutil.iso(row["missed_at"]),
    }


def get_callback(conn, callback_id: int) -> dict:
    row = conn.execute("SELECT * FROM callbacks WHERE id = ?", (callback_id,)).fetchone()
    if row is None:
        raise NotFound(f"回拨预约 {callback_id} 不存在")
    return _callback_dict(row)


def _callback_row(conn, callback_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM callbacks WHERE id = ?", (callback_id,)).fetchone()
    if row is None:
        raise NotFound(f"回拨预约 {callback_id} 不存在")
    return row


def list_callbacks(conn, ticket_id: int | None = None, status: str | None = None,
                   team_id: int | None = None, agent_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM callbacks"
    clauses, args = [], []
    if ticket_id is not None:
        clauses.append("ticket_id = ?")
        args.append(ticket_id)
    if status is not None:
        if status not in ACTIVE_STATUSES + TERMINAL_STATUSES:
            raise Conflict(f"未知状态 {status}")
        clauses.append("status = ?")
        args.append(status)
    if team_id is not None:
        clauses.append("team_id = ?")
        args.append(team_id)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        args.append(agent_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [_callback_dict(r) for r in conn.execute(sql, args).fetchall()]


def active_callback_for_ticket(conn, ticket_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM callbacks WHERE ticket_id = ? AND status IN ('pending', 'in_progress')"
        " ORDER BY id DESC LIMIT 1",
        (ticket_id,),
    ).fetchone()
    return _callback_dict(row) if row else None


# --------------------------------------------------------------------------
# 预约 / 改期 / 取消（客服）
# --------------------------------------------------------------------------


def _require_bookable_ticket(conn, ticket_id: int) -> sqlite3.Row:
    t = service._ticket_row(conn, ticket_id)
    if t["accepted_at"] is None:
        raise Conflict("工单尚未受理，不能预约回拨")
    if t["status"] == "resolved":
        raise Conflict("工单已结案，不能预约回拨")
    return t


def book_callback(conn, ticket_id: int, skill: str, start: int, end: int,
                  actor: str, now: int, agent_id: int | None = None) -> dict:
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    t = _require_bookable_ticket(conn, ticket_id)
    if not isinstance(skill, str) or not skill.strip():
        raise Conflict("所需技能不能为空")
    skill = skill.strip()
    validate_window(start, end)
    if start < now:
        raise Conflict("新预约的开始不能早于当前业务时刻")
    if active_callback_for_ticket(conn, ticket_id) is not None:
        raise Conflict("该工单已有未开始或进行中的回拨")

    candidate = {
        "id": -1, "ticket_id": ticket_id, "team_id": int(t["current_team_id"]),
        "skill": skill, "start_minute": start, "end_minute": end,
        "status": "pending", "agent_id": None,
    }
    plan = feasibility_plan(conn, now, extra=candidate)
    assigned = plan.pop(-1)
    if agent_id is not None and assigned != agent_id:
        # 明确指定坐席时，该坐席必须本身合格且不与他人重叠
        if not _agent_can_take(conn, agent_id, int(t["current_team_id"]), skill, start, end, now):
            raise Conflict("指定坐席无法覆盖整个窗口、不具备资格或时间冲突")
        assigned = agent_id
    # 求解器可能为腾出容量而重排既有 pending 预约，一并落库
    _persist_reassignments(conn, plan)
    cur = conn.execute(
        """INSERT INTO callbacks
           (ticket_id, team_id, skill, start_minute, end_minute, status, agent_id, booked_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (ticket_id, int(t["current_team_id"]), skill, start, end, assigned, now),
    )
    callback_id = int(cur.lastrowid)
    service.record_event(
        conn, ticket_id, "callback_booked", actor, now,
        {"callback_id": callback_id, "skill": skill,
         "window": [start, end], "agent_id": assigned},
    )
    # 重新读取：求解器可能同时重排了既有预约，本预约也以最终落库状态为准
    return get_callback(conn, callback_id)


def _persist_reassignments(conn, plan: dict[int, int]) -> None:
    """把求解器对既有 pending 预约的重新分配落库（in_progress 不会出现在 plan 中）。"""
    for callback_id, agent_id in plan.items():
        conn.execute(
            "UPDATE callbacks SET agent_id = ? WHERE id = ? AND status = 'pending'",
            (agent_id, callback_id),
        )


def _agent_can_take(conn, agent_id: int, team_id: int, skill: str,
                    start: int, end: int, now: int, exclude_id: int | None = None) -> bool:
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None or int(row["team_id"]) != team_id:
        return False
    has_skill = conn.execute(
        "SELECT 1 FROM agent_skills WHERE agent_id = ? AND skill = ?", (agent_id, skill)
    ).fetchone()
    if has_skill is None:
        return False
    avail = _merge([
        (int(r["start_minute"]), int(r["end_minute"]))
        for r in conn.execute(
            "SELECT start_minute, end_minute FROM agent_availability WHERE agent_id = ?", (agent_id,))
    ])
    if not _covers(avail, start, end):
        return False
    sql = ("SELECT start_minute, end_minute FROM callbacks"
           " WHERE agent_id = ? AND status IN ('pending', 'in_progress')")
    args: list = [agent_id]
    if exclude_id is not None:
        sql += " AND id <> ?"
        args.append(exclude_id)
    for r in conn.execute(sql, args).fetchall():
        if _overlaps(start, end, int(r["start_minute"]), int(r["end_minute"])):
            return False
    return True


def reschedule_callback(conn, callback_id: int, start: int, end: int,
                        actor: str, now: int) -> dict:
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    row = _callback_row(conn, callback_id)
    if row["status"] == "in_progress":
        raise Conflict("回拨已开始，不能改期")
    if row["status"] != "pending":
        raise Conflict(f"当前状态 {row['status']} 不能改期")
    validate_window(start, end)
    if start < now:
        raise Conflict("改期后的开始不能早于当前业务时刻")

    candidate = {
        "id": -1, "ticket_id": int(row["ticket_id"]), "team_id": int(row["team_id"]),
        "skill": row["skill"], "start_minute": start, "end_minute": end,
        "status": "pending", "agent_id": None,
    }
    # 把原预约排除后整体求解：允许在所有 pending 间重新分配；不可行则外层事务回滚，
    # 原窗口、原坐席与原承诺完全不变。
    plan = feasibility_plan(conn, now, extra=candidate, exclude_ids={callback_id})
    assigned = plan.pop(-1)
    _persist_reassignments(conn, plan)
    conn.execute(
        "UPDATE callbacks SET"
        " start_minute = ?, end_minute = ?, agent_id = ?, reschedule_count = reschedule_count + 1"
        " WHERE id = ? AND status = 'pending'",
        (start, end, assigned, callback_id),
    )
    service.record_event(
        conn, int(row["ticket_id"]), "callback_rescheduled", actor, now,
        {"callback_id": callback_id, "window": [start, end], "agent_id": assigned},
    )
    return get_callback(conn, callback_id)


def cancel_callback(conn, callback_id: int, actor: str, now: int, reason: str | None = None) -> dict:
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    row = _callback_row(conn, callback_id)
    if row["status"] == "in_progress":
        raise Conflict("回拨已开始，不能取消；须先登记结束")
    if row["status"] != "pending":
        raise Conflict(f"当前状态 {row['status']} 不能取消")
    conn.execute(
        "UPDATE callbacks SET status = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE id = ?",
        (now, reason or "cancelled", callback_id),
    )
    service.record_event(
        conn, int(row["ticket_id"]), "callback_cancelled", actor, now,
        {"callback_id": callback_id, "reason": reason or "cancelled"},
    )
    return get_callback(conn, callback_id)


# --------------------------------------------------------------------------
# 开始 / 结束（坐席）
# --------------------------------------------------------------------------


def start_callback(conn, callback_id: int, agent_id: int, actor: str,
                   now: int, at: int | None = None) -> dict:
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    row = _callback_row(conn, callback_id)
    if row["status"] != "pending":
        raise Conflict(f"当前状态 {row['status']} 不能开始回拨")
    at = now if at is None else at
    if at > now:
        raise Conflict("开始时刻不能晚于当前业务时刻")
    s, e = int(row["start_minute"]), int(row["end_minute"])
    if not (s <= at < e):
        raise Conflict("只能在约定窗口内开始回拨")

    # 开始不能排挤其他已确认预约：执行坐席必须合格且与该坐席其他有效预约不重叠
    if not _agent_can_take(conn, agent_id, int(row["team_id"]), row["skill"], s, e, now,
                           exclude_id=callback_id):
        raise Conflict("该坐席不具备资格、无法覆盖整个窗口或时间冲突")

    conn.execute(
        "UPDATE callbacks SET status = 'in_progress', agent_id = ?, started_at = ?"
        " WHERE id = ? AND status = 'pending'",
        (agent_id, at, callback_id),
    )
    service.record_event(
        conn, int(row["ticket_id"]), "callback_started", actor, at,
        {"callback_id": callback_id, "agent_id": agent_id},
    )
    return get_callback(conn, callback_id)


def end_callback(conn, callback_id: int, actor: str, now: int, at: int | None = None) -> dict:
    _bump_clock(conn, now)
    row = _callback_row(conn, callback_id)
    if row["status"] != "in_progress":
        raise Conflict(f"当前状态 {row['status']} 无需登记结束")
    if row["agent_id"] is None:
        raise Conflict("进行中的回拨缺少执行坐席")
    at = now if at is None else at
    if at > now:
        raise Conflict("结束时刻不能晚于当前业务时刻")
    if at < int(row["started_at"]):
        raise Conflict("结束时刻不能早于开始时刻")
    conn.execute(
        "UPDATE callbacks SET status = 'completed', ended_at = ? WHERE id = ?",
        (at, callback_id),
    )
    service.record_event(
        conn, int(row["ticket_id"]), "callback_ended", actor, at,
        {"callback_id": callback_id, "agent_id": int(row["agent_id"]),
         "window": [int(row["start_minute"]), int(row["end_minute"])]},
    )
    return get_callback(conn, callback_id)


# --------------------------------------------------------------------------
# 与工单转组 / 结案的联动
# --------------------------------------------------------------------------


def before_transfer(conn, ticket_id: int, to_team_id: int, now: int) -> None:
    """转组前置检查/处理。

    - 已开始未结束：拒绝转组（须先结束回拨）。
    - 未过期且未开始：保留客户窗口与技能，由目标组承接；目标组安排不下则拒绝，
      原预约归属与坐席分配不变。
    - 已错过/已取消/已结束：不随工单转移。
    """
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    active = conn.execute(
        "SELECT * FROM callbacks WHERE ticket_id = ? AND status IN ('pending', 'in_progress')"
        " ORDER BY id",
        (ticket_id,),
    ).fetchall()
    in_progress = [r for r in active if r["status"] == "in_progress"]
    if in_progress:
        raise Conflict("回拨已开始但尚未登记结束，不能转组")
    pending = [r for r in active if r["status"] == "pending"]
    if not pending:
        return
    # 用承接组覆盖做整体可行性预判：本工单的 pending 改由目标组安排，
    # 其余所有尚有效预约保持不变。
    override = {int(r["id"]): to_team_id for r in pending}
    plan = feasibility_plan(conn, now, team_override=override)
    for row in pending:
        conn.execute(
            "UPDATE callbacks SET team_id = ?, agent_id = ? WHERE id = ?",
            (to_team_id, plan[int(row["id"])], int(row["id"])),
        )
        service.record_event(
            conn, ticket_id, "callback_transferred", "system", now,
            {"callback_id": int(row["id"]), "to_team_id": to_team_id,
             "agent_id": plan[int(row["id"])]},
        )


def before_resolve(conn, ticket_id: int, now: int) -> None:
    """结案前置处理：取消仍未开始且未过期的预约；已开始未结束则拒绝结案。"""
    _bump_clock(conn, now)
    sweep_missed(conn, now)
    active = conn.execute(
        "SELECT * FROM callbacks WHERE ticket_id = ? AND status IN ('pending', 'in_progress')"
        " ORDER BY id",
        (ticket_id,),
    ).fetchall()
    for row in active:
        if row["status"] == "in_progress":
            raise Conflict("回拨已开始但尚未登记结束，不能结案")
        if int(row["end_minute"]) > now:
            conn.execute(
                "UPDATE callbacks SET status = 'cancelled', cancelled_at = ?,"
                " cancel_reason = 'ticket_resolved' WHERE id = ?",
                (now, int(row["id"])),
            )
            service.record_event(
                conn, ticket_id, "callback_cancelled", "system", now,
                {"callback_id": int(row["id"]), "reason": "ticket_resolved"},
            )


def callbacks_for_ticket(conn, ticket_id: int) -> list[dict]:
    return [
        _callback_dict(r)
        for r in conn.execute("SELECT * FROM callbacks WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    ]
