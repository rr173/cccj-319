"""回拨预约端到端测试。

覆盖：坐席档案与全局可行性、预约/改期/取消、开始/结束、错过与时钟高水位、
转组/结案联动、并发与“拒绝不留部分更新”。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    app = create_app(path)
    c = TestClient(app)
    yield c
    try:
        c.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def team(client, name="A", **kw):
    r = client.post("/teams", json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def clock(client, at):
    r = client.post("/clock", json={"at": at})
    assert r.status_code == 200, r.text
    return r.json()


def ticket(client, team_id, title="t", **kw):
    r = client.post("/tickets", json={"title": title, "team_id": team_id, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def agent(client, team_id, name, skills=("billing",), windows=None):
    windows = windows or [["2026-01-01T08:00:00Z", "2026-01-31T20:00:00Z"]]
    r = client.post("/agents", json={
        "name": name, "team_id": team_id, "skills": list(skills), "availability": windows,
    })
    assert r.status_code == 201, r.text
    return r.json()


def book(client, tid, skill, start, end, **kw):
    return client.post(f"/tickets/{tid}/callbacks",
                       json={"skill": skill, "start": start, "end": end, **kw})


def cb(client, callback_id):
    r = client.get(f"/callbacks/{callback_id}")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# 坐席档案
# --------------------------------------------------------------------------


def test_agent_requires_team_skill_and_availability(client):
    t = team(client)
    r = client.post("/agents", json={"name": "a", "team_id": t["id"], "skills": [],
                                     "availability": [["2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z"]]})
    assert r.status_code in (409, 422)
    r = client.post("/agents", json={"name": "a", "team_id": t["id"], "skills": ["x"],
                                     "availability": [["2026-01-01T11:00:00Z", "2026-01-01T10:00:00Z"]]})
    assert r.status_code == 409
    r = client.post("/agents", json={"name": "a", "team_id": 999, "skills": ["x"],
                                     "availability": [["2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z"]]})
    assert r.status_code == 404
    a = agent(client, t["id"], "a1", skills=["billing", "tech"])
    assert set(a["skills"]) == {"billing", "tech"}
    assert a["availability"][0]["start_minute"] < a["availability"][0]["end_minute"]


def test_overlapping_availability_rejected(client):
    t = team(client)
    r = client.post("/agents", json={"name": "a", "team_id": t["id"], "skills": ["x"], "availability": [
        ["2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z"],
        ["2026-01-01T10:30:00Z", "2026-01-01T12:00:00Z"],
    ]})
    assert r.status_code == 409


# --------------------------------------------------------------------------
# 预约：窗口、唯一性、资格、容量与整窗覆盖
# --------------------------------------------------------------------------


def test_book_basic_and_ticket_detail(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    r = book(client, tk["id"], "billing", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert data["agent_id"] == a["id"]
    assert data["window_start"] == "2026-01-01T10:00:00Z"
    detail = client.get(f"/tickets/{tk['id']}").json()
    assert detail["active_callback"]["id"] == data["id"]
    assert detail["active_callback"]["window_end"] == "2026-01-01T10:30:00Z"
    assert [c["id"] for c in detail["callbacks"]] == [data["id"]]


def test_window_must_be_half_open_and_not_in_past(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    r = book(client, tk["id"], "billing", "2026-01-01T10:30:00Z", "2026-01-01T10:30:00Z")
    assert r.status_code == 409
    r = book(client, tk["id"], "billing", "2026-01-01T08:00:00Z", "2026-01-01T08:30:00Z")
    assert r.status_code == 409 and "当前业务时刻" in r.json()["detail"]


def test_integer_minute_input_accepted(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    now = client.get("/clock").json()["now_minute"]
    r = book(client, tk["id"], "billing", now + 60, now + 90)
    assert r.status_code == 201, r.text
    assert r.json()["start_minute"] == now + 60


def test_only_accepted_open_ticket_may_book(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"], accept=False)
    r = book(client, tk["id"], "billing", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z")
    assert r.status_code == 409
    client.post(f"/tickets/{tk['id']}/accept", json={})
    assert book(client, tk["id"], "billing", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z").status_code == 201
    client.post(f"/tickets/{tk['id']}/resolve", json={})
    r = book(client, tk["id"], "billing", "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    assert r.status_code == 409


def test_one_active_callback_per_ticket(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    book(client, tk["id"], "billing", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z")
    r = book(client, tk["id"], "billing", "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    assert r.status_code == 409 and "已有" in r.json()["detail"]


def test_skill_team_and_full_window_coverage_required(client):
    a_team = team(client, "A")
    b_team = team(client, "B")
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, a_team["id"], "a1", skills=["tech"],
          windows=[["2026-01-01T10:00:00Z", "2026-01-01T11:00:00Z"]])
    agent(client, b_team["id"], "b1", skills=["billing"],
          windows=[["2026-01-01T08:00:00Z", "2026-01-01T20:00:00Z"]])
    tk = ticket(client, a_team["id"])
    # 技能不符
    r = book(client, tk["id"], "billing", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z")
    assert r.status_code == 409
    # 窗口超出坐席可用时段（不能分段换人）
    r = book(client, tk["id"], "tech", "2026-01-01T10:30:00Z", "2026-01-01T11:30:00Z")
    assert r.status_code == 409
    # 可行
    r = book(client, tk["id"], "tech", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z")
    assert r.status_code == 201


def test_overlapping_bookings_spread_across_distinct_agents(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a1 = agent(client, t["id"], "a1")
    a2 = agent(client, t["id"], "a2")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    w = ("2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    c1 = book(client, tk1["id"], "billing", *w).json()
    c2 = book(client, tk2["id"], "billing", *w).json()
    assert cb(client, c1["id"])["agent_id"] != cb(client, c2["id"])
    # 第三个重叠预约无人可接
    tk3 = ticket(client, t["id"], "t3")
    r = book(client, tk3["id"], "billing", *w)
    assert r.status_code == 409


def test_new_booking_may_reassign_existing_pending_to_make_room(client):
    """求解器重排：后到的预约可触发既有 pending 换坐席，只要整体可行。"""
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a1 = agent(client, t["id"], "a1")
    a2 = agent(client, t["id"], "a2")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    # 两段背靠背，初始都可解（具体落位由求解器决定）
    c1 = book(client, tk1["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    c2 = book(client, tk2["id"], "billing",
              "2026-01-02T10:30:00Z", "2026-01-02T11:00:00Z").json()
    # 与 c1 重叠的新约：求解器可重排既有 pending 腾出容量，整体可行即接受
    tk3 = ticket(client, t["id"], "t3")
    r = book(client, tk3["id"], "billing",
             "2026-01-02T10:00:00Z", "2026-01-02T10:15:00Z")
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    # 三笔预约（两笔重叠 + 一笔邻接）必须恰好分布在两名坐席且无重叠
    rows = client.get("/callbacks", params={"status": "pending"}).json()
    by_agent = {}
    for row in rows:
        by_agent.setdefault(row["agent_id"], []).append((row["start_minute"], row["end_minute"]))
    assert set(by_agent) == {a1["id"], a2["id"]}
    for windows in by_agent.values():
        windows.sort()
        for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
            assert s2 >= e1
    assert r.json()["id"] == new_id


# --------------------------------------------------------------------------
# 改期 / 取消
# --------------------------------------------------------------------------


def test_reschedule_rules(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    # 坐席仅在 10:00-12:00 可用，14:00 的窗口无人能覆盖
    a1 = agent(client, t["id"], "a1",
               windows=[["2026-01-02T10:00:00Z", "2026-01-02T12:00:00Z"]])
    agent(client, t["id"], "a2",
          windows=[["2026-01-02T10:00:00Z", "2026-01-02T12:00:00Z"]])
    tk1 = ticket(client, t["id"])
    tk2 = ticket(client, t["id"], "t2")
    c1 = book(client, tk1["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    book(client, tk2["id"], "billing",
         "2026-01-02T10:30:00Z", "2026-01-02T11:00:00Z")
    # 改期到过去（早于当前业务时刻）-> 拒绝
    r = client.post(f"/callbacks/{c1['id']}/reschedule",
                    json={"start": "2026-01-01T08:00:00Z", "end": "2026-01-01T08:30:00Z"})
    assert r.status_code == 409 and "当前业务时刻" in r.json()["detail"]
    # 改期到无人能覆盖的窗口 -> 拒绝，原窗口与计数不变
    before = cb(client, c1["id"])
    r = client.post(f"/callbacks/{c1['id']}/reschedule",
                    json={"start": "2026-01-02T14:00:00Z", "end": "2026-01-02T15:00:00Z"})
    assert r.status_code == 409
    after = cb(client, c1["id"])
    assert after["start_minute"] == before["start_minute"]
    assert after["reschedule_count"] == before["reschedule_count"] == 0
    # 可行改期：与另一预约不重叠
    r = client.post(f"/callbacks/{c1['id']}/reschedule",
                    json={"start": "2026-01-02T11:00:00Z", "end": "2026-01-02T11:30:00Z"})
    assert r.status_code == 200
    assert cb(client, c1["id"])["reschedule_count"] == 1


def test_cancel_releases_capacity_and_allows_rebook(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    w = ("2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    c1 = book(client, tk1["id"], "billing", *w).json()
    assert book(client, tk2["id"], "billing", *w).status_code == 409
    r = client.post(f"/callbacks/{c1['id']}/cancel", json={"reason": "客户改期"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert book(client, tk2["id"], "billing", *w).status_code == 201
    # 已取消不能再取消/改期
    assert client.post(f"/callbacks/{c1['id']}/cancel", json={}).status_code == 409
    assert client.post(f"/callbacks/{c1['id']}/reschedule",
                       json={"start": w[0], "end": w[1]}).status_code == 409


# --------------------------------------------------------------------------
# 开始 / 结束
# --------------------------------------------------------------------------


def test_start_only_inside_window_and_locks_agent(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    # 窗口外开始
    r = client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    assert r.status_code == 409
    clock(client, "2026-01-02T10:30:00Z")  # 半开：终点不含
    r = client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    assert r.status_code == 409
    # 已错过，回放时钟也不能开始
    clock(client, "2026-01-02T10:05:00Z")
    r = client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    assert r.status_code == 409 and cb(client, c1["id"])["status"] == "missed"


def test_start_end_lifecycle_and_immutability(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    clock(client, "2026-01-02T10:00:00Z")
    r = client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    assert r.json()["agent_id"] == a["id"] and r.json()["started_at"] is not None
    # 开始后不能改期/取消/重复开始
    assert client.post(f"/callbacks/{c1['id']}/reschedule",
                       json={"start": "2026-01-03T10:00:00Z",
                             "end": "2026-01-03T10:30:00Z"}).status_code == 409
    assert client.post(f"/callbacks/{c1['id']}/cancel", json={}).status_code == 409
    assert client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]}).status_code == 409
    # 提前结束释放余下时段（显式 at 不能晚于业务时刻，先推进时钟）
    clock(client, "2026-01-02T10:10:00Z")
    r = client.post(f"/callbacks/{c1['id']}/end", json={"at": "2026-01-02T10:10:00Z"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert r.json()["ended_at"] == "2026-01-02T10:10:00Z"
    # 释放的容量可被新约使用（与原窗口重叠）
    tk2 = ticket(client, t["id"], "t2")
    assert book(client, tk2["id"], "billing",
                "2026-01-02T10:15:00Z", "2026-01-02T10:20:00Z").status_code == 201


def test_late_end_registration_allowed_after_window(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    clock(client, "2026-01-02T10:05:00Z")
    client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    # 窗口到点后容量自然释放，但仍允许补记结束
    clock(client, "2026-01-03T08:00:00Z")
    detail = client.get(f"/tickets/{tk['id']}").json()
    assert detail["active_callback"]["status"] == "in_progress"
    r = client.post(f"/callbacks/{c1['id']}/end", json={})
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_start_cannot_displace_other_confirmed_callbacks(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a1 = agent(client, t["id"], "a1")
    a2 = agent(client, t["id"], "a2")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    w = ("2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    c1 = book(client, tk1["id"], "billing", *w).json()
    c2 = book(client, tk2["id"], "billing", *w).json()
    other = cb(client, c2["id"])["agent_id"]
    # 让 c1 的坐席用另一坐席开始 c2 不可能；这里强制 c1 用 c2 的执行坐席开始 -> 冲突
    clock(client, "2026-01-02T10:05:00Z")
    r = client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": other})
    assert r.status_code == 409
    # 用自己的坐席可以开始
    assert client.post(f"/callbacks/{c1['id']}/start",
                       json={"agent_id": cb(client, c1["id"])["agent_id"]}).status_code == 200


# --------------------------------------------------------------------------
# 档案调整：全局可行性、执行坐席保护、拒绝不留部分更新
# --------------------------------------------------------------------------


def test_profile_change_reassigns_pending_but_rejects_infeasible(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a1 = agent(client, t["id"], "a1")
    a2 = agent(client, t["id"], "a2")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    c1 = book(client, tk1["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    c2 = book(client, tk2["id"], "billing",
              "2026-01-02T10:30:00Z", "2026-01-02T11:00:00Z").json()
    # 摘掉 a1 技能：两段不重叠，可整体挪到 a2
    r = client.patch(f"/agents/{a1['id']}", json={"skills": ["other"]})
    assert r.status_code == 200
    assert cb(client, c1["id"])["agent_id"] == a2["id"]
    assert cb(client, c2["id"])["agent_id"] == a2["id"]
    # 再摘 a2 技能 => 无人可接，拒绝且无部分更新
    r = client.patch(f"/agents/{a2['id']}", json={"skills": ["nope"]})
    assert r.status_code == 409
    assert client.get(f"/agents/{a2['id']}").json()["skills"] == ["billing"]
    assert cb(client, c1["id"])["agent_id"] == a2["id"]


def test_profile_change_cannot_break_in_progress_coverage(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    clock(client, "2026-01-02T10:05:00Z")
    client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    # 缩窄可用时段，使其不再覆盖原窗口
    r = client.patch(f"/agents/{a['id']}", json={
        "availability": [["2026-01-02T10:30:00Z", "2026-01-02T12:00:00Z"]]})
    assert r.status_code == 409
    # 移除技能 / 调组同样被拒
    assert client.patch(f"/agents/{a['id']}", json={"skills": ["x"]}).status_code == 409
    other = team(client, "B")
    assert client.patch(f"/agents/{a['id']}", json={"team_id": other["id"]}).status_code == 409
    # 档案保持原样
    assert client.get(f"/agents/{a['id']}").json()["skills"] == ["billing"]
    assert cb(client, c1["id"])["status"] == "in_progress"


def test_move_agent_team_then_only_new_team_callbacks_eligible(client):
    t = team(client, "A")
    other = team(client, "B")
    clock(client, "2026-01-01T09:00:00Z")
    narrow = [["2026-01-02T10:00:00Z", "2026-01-02T12:00:00Z"]]
    a1 = agent(client, t["id"], "a1", windows=narrow)
    a2 = agent(client, t["id"], "a2", windows=narrow)
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    # B 组无人：把 a1 调去 B 后，A 组仍有 a2 兜底，预约改挂 a2 => 可行
    r = client.patch(f"/agents/{a1['id']}", json={"team_id": other["id"]})
    assert r.status_code == 200
    assert cb(client, c1["id"])["agent_id"] == a2["id"]
    # 再把唯一兜底的 a2 也调走 => A 组无人承接 => 拒绝，归属不变
    r = client.patch(f"/agents/{a2['id']}", json={"team_id": other["id"]})
    assert r.status_code == 409
    assert client.get(f"/agents/{a2['id']}").json()["team_id"] == t["id"]
    assert cb(client, c1["id"])["agent_id"] == a2["id"]
    # B 组补一名同技能坐席也无助于 A 组预约（坐席必须属于工单负责组），仍拒绝
    b1 = agent(client, other["id"], "b1", windows=narrow)
    assert client.patch(f"/agents/{a2['id']}", json={"team_id": other["id"]}).status_code == 409
    assert cb(client, c1["id"])["team_id"] == t["id"]
    # 但工单转组到 B 后，B 组坐席可承接（窗口与技能保留）
    r = client.post(f"/tickets/{tk['id']}/transfer", json={"to_team_id": other["id"]})
    assert r.status_code == 200
    moved = cb(client, c1["id"])
    assert moved["team_id"] == other["id"]
    assert moved["agent_id"] in {a1["id"], a2["id"], b1["id"]}
    assert moved["skill"] == "billing"
    assert moved["start_minute"] == c1["start_minute"]


# --------------------------------------------------------------------------
# 错过与时钟高水位
# --------------------------------------------------------------------------


def test_missed_releases_capacity_and_survives_clock_rollback(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    clock(client, "2026-01-02T10:30:00Z")
    assert cb(client, c1["id"])["status"] == "missed"
    # 容量已释放：同工单可新约
    r = book(client, tk["id"], "billing",
             "2026-01-02T11:00:00Z", "2026-01-02T11:30:00Z")
    assert r.status_code == 201
    # 时钟回放不能复活
    clock(client, "2026-01-02T10:05:00Z")
    assert cb(client, c1["id"])["status"] == "missed"
    assert client.post(f"/callbacks/{c1['id']}/start",
                       json={"agent_id": a["id"]}).status_code == 409
    assert client.post(f"/callbacks/{c1['id']}/reschedule",
                       json={"start": "2026-01-02T14:00:00Z",
                             "end": "2026-01-02T14:30:00Z"}).status_code == 409


def test_callback_operation_advances_high_water(client):
    t = team(client)
    clock(client, "2026-01-05T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-05T10:00:00Z", "2026-01-05T10:30:00Z").json()
    # 时钟回放到预约之前，业务时刻仍不低于已到达的高水位
    clock(client, "2026-01-01T00:00:00Z")
    # 新约开始仍不得早于高水位
    r = book(client, ticket(client, t["id"], "t2")["id"], "billing",
             "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")
    assert r.status_code == 409 and "当前业务时刻" in r.json()["detail"]
    assert cb(client, c1["id"])["status"] == "pending"  # 未被错误错过/复活


# --------------------------------------------------------------------------
# 转组 / 结案联动
# --------------------------------------------------------------------------


def test_transfer_carries_pending_callback_or_rejects(client):
    a_team = team(client, "A")
    b_team = team(client, "B")
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, a_team["id"], "a1")
    tk = ticket(client, a_team["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    # B 组无人能接 => 不转组
    r = client.post(f"/tickets/{tk['id']}/transfer", json={"to_team_id": b_team["id"]})
    assert r.status_code == 409
    detail = client.get(f"/tickets/{tk['id']}").json()
    assert detail["team_id"] == a_team["id"]
    assert detail["active_callback"]["team_id"] == a_team["id"]
    # B 组补足坐席 => 预约保留窗口与技能随组转移
    b = agent(client, b_team["id"], "b1")
    r = client.post(f"/tickets/{tk['id']}/transfer", json={"to_team_id": b_team["id"]})
    assert r.status_code == 200
    moved = cb(client, c1["id"])
    assert moved["team_id"] == b_team["id"]
    assert moved["agent_id"] == b["id"]
    assert moved["skill"] == "billing"
    assert moved["start_minute"] == c1["start_minute"]


def test_in_progress_callback_blocks_transfer_and_resolve(client):
    t = team(client)
    other = team(client, "B")
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    agent(client, other["id"], "b1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    clock(client, "2026-01-02T10:05:00Z")
    client.post(f"/callbacks/{c1['id']}/start",
                json={"agent_id": cb(client, c1["id"])["agent_id"]})
    assert client.post(f"/tickets/{tk['id']}/transfer",
                       json={"to_team_id": other["id"]}).status_code == 409
    assert client.post(f"/tickets/{tk['id']}/resolve", json={}).status_code == 409
    # 结束后才允许转组/结案
    client.post(f"/callbacks/{c1['id']}/end", json={})
    assert client.post(f"/tickets/{tk['id']}/resolve", json={}).status_code == 200


def test_resolve_cancels_pending_unexpired_callback(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    c1 = book(client, tk["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    r = client.post(f"/tickets/{tk['id']}/resolve", json={})
    assert r.status_code == 200
    data = cb(client, c1["id"])
    assert data["status"] == "cancelled"
    assert data["cancel_reason"] == "ticket_resolved"
    assert client.get(f"/tickets/{tk['id']}").json()["active_callback"] is None


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def test_callback_list_filters(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk1 = ticket(client, t["id"], "t1")
    tk2 = ticket(client, t["id"], "t2")
    c1 = book(client, tk1["id"], "billing",
              "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z").json()
    c2 = book(client, tk2["id"], "billing",
              "2026-01-03T10:00:00Z", "2026-01-03T10:30:00Z").json()
    clock(client, "2026-01-02T10:05:00Z")
    client.post(f"/callbacks/{c1['id']}/start", json={"agent_id": a["id"]})
    client.post(f"/callbacks/{c1['id']}/end", json={})
    assert {x["id"] for x in client.get("/callbacks", params={"status": "completed"}).json()} == {c1["id"]}
    assert {x["id"] for x in client.get("/callbacks", params={"status": "pending"}).json()} == {c2["id"]}
    assert {x["id"] for x in client.get("/callbacks", params={"agent_id": a["id"]}).json()} == {c1["id"], c2["id"]}
    assert {x["id"] for x in client.get(f"/tickets/{tk1['id']}/callbacks").json()} == {c1["id"]}


# --------------------------------------------------------------------------
# 并发：串行化写者，拒绝的变更不留部分更新
# --------------------------------------------------------------------------


def test_concurrent_bookings_serialized(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    agent(client, t["id"], "a1")
    tickets = [ticket(client, t["id"], f"t{i}")["id"] for i in range(2)]
    results = {}

    def make_booking(idx):
        results[idx] = book(client, tickets[idx], "billing",
                            "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")

    threads = [threading.Thread(target=make_booking, args=(i,)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [201, 409]
    active = client.get("/callbacks", params={"status": "pending"}).json()
    assert len(active) == 1


def test_concurrent_profile_change_and_booking_atomic(client):
    t = team(client)
    clock(client, "2026-01-01T09:00:00Z")
    a = agent(client, t["id"], "a1")
    tk = ticket(client, t["id"])
    results = {}

    def change_skill():
        results["patch"] = client.patch(f"/agents/{a['id']}", json={"skills": ["other"]})

    def make_booking():
        results["book"] = book(client, tk["id"], "billing",
                               "2026-01-02T10:00:00Z", "2026-01-02T10:30:00Z")

    threads = [threading.Thread(target=change_skill), threading.Thread(target=make_booking)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # 二者必然一个成功一个失败，不会出现“技能改了但预约还挂着无技能坐席”
    if results["patch"].status_code == 200:
        assert results["book"].status_code == 409
        assert client.get(f"/agents/{a['id']}").json()["skills"] == ["other"]
    else:
        assert results["book"].status_code == 201
        assert client.get(f"/agents/{a['id']}").json()["skills"] == ["billing"]
        assert client.get("/callbacks", params={"status": "pending"}).json()[0]["agent_id"] == a["id"]
