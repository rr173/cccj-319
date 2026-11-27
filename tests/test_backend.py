"""端到端测试：覆盖预算冻结、日历、暂停、转组、更正与升级。"""

from __future__ import annotations

import os
import sys
import tempfile

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


def team(c, name="A", rb=60, sb=480):
    r = c.post("/teams", json={"name": name, "response_budget_min": rb, "resolution_budget_min": sb})
    assert r.status_code == 201, r.text
    return r.json()


def clock(c, at):
    r = c.post("/clock", json={"at": at})
    assert r.status_code == 200, r.text
    return r.json()


def cal(c, team_id, day, intervals):
    r = c.put(f"/teams/{team_id}/calendar/{day}", json={"team_id": team_id, "day": day, "intervals": intervals})
    assert r.status_code == 200, r.text
    return r.json()


def ticket(c, team_id, title="t", **kw):
    r = c.post("/tickets", json={"title": title, "team_id": team_id, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def get(c, tid):
    r = c.get(f"/tickets/{tid}")
    assert r.status_code == 200, r.text
    return r.json()


def post(c, path, **body):
    return c.post(path, json=body)


# --------------------------------------------------------------------------
# 基础：预算冻结与工作分钟
# --------------------------------------------------------------------------


def test_budget_frozen_at_registration(client):
    t1 = team(client, "A", rb=60, sb=480)
    t2 = team(client, "B", rb=30, sb=60)
    clock(client, "2026-01-01T09:00:00Z")
    tk = ticket(client, t1["id"])
    assert tk["response"]["budget_min"] == 60
    assert tk["resolution"]["budget_min"] == 480
    # 事后改团队默认预算不影响已登记工单
    client.patch(f"/teams/{t1['id']}", json={"response_budget_min": 5, "resolution_budget_min": 5})
    assert get(client, tk["id"])["response"]["budget_min"] == 60


def test_working_minutes_exclude_off_hours(client):
    t = team(client, "A", rb=60)
    clock(client, "2026-01-01T09:00:00Z")
    # 09:00-12:00 与 13:00-18:00
    cal(client, t["id"], "2026-01-01", [[540, 720], [780, 1080]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    v = get(client, tk["id"])["response"]
    assert v["elapsed_min"] == 90  # 09:00-10:30
    assert v["current"]["breach"] is True
    assert v["over_by_min"] == 30
    assert v["remaining_min"] == 0


def test_half_open_interval_boundary(client):
    t = team(client, "A", rb=60)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])  # 09:00 包含，10:00 不含
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:00:00Z")
    assert get(client, tk["id"])["response"]["elapsed_min"] == 60


def test_exact_budget_is_not_breach(client):
    t = team(client, "A", rb=60)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:00:00Z")
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T10:00:00Z")
    assert r.status_code == 200, r.text
    v = r.json()["response"]
    assert v["elapsed_min"] == 60
    assert v["current"]["breach"] is False
    assert v["original"]["breach"] is False
    assert r.json()["escalations"] == []


def test_incomplete_query_gives_spent_and_remaining(client):
    t = team(client, "A", rb=120, sb=300)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:00:00Z")
    v = get(client, tk["id"])["response"]
    assert v["elapsed_min"] == 60 and v["remaining_min"] == 60
    assert v["completed"] is False


# --------------------------------------------------------------------------
# 响应 / 解决分别管理
# --------------------------------------------------------------------------


def test_response_and_resolution_managed_separately(client):
    t = team(client, "A", rb=30, sb=120)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:00:00Z")
    d = get(client, tk["id"])
    assert d["response"]["current"]["breach"] is True   # 60 > 30
    assert d["resolution"]["current"]["breach"] is False  # 60 <= 120
    assert [e["dimension"] for e in d["escalations"]] == ["response"]


def test_agent_note_does_not_satisfy_response(client):
    t = team(client, "A", rb=30)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T09:10:00Z")
    post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=False, content="内部记录",
         at="2026-01-01T09:10:00Z")
    assert get(client, tk["id"])["response"]["completed"] is False
    clock(client, "2026-01-01T09:20:00Z")
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True,
             at="2026-01-01T09:20:00Z")
    v = r.json()["response"]
    assert v["completed"] is True and v["elapsed_min"] == 20


# --------------------------------------------------------------------------
# 暂停：只有客服发起 + 关联客户回复才结束
# --------------------------------------------------------------------------


def test_wait_pauses_resolution_only(client):
    t = team(client, "A", rb=60, sb=120)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T09:10:00Z")
    post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T09:10:00Z")
    clock(client, "2026-01-01T09:30:00Z")
    post(client, f"/tickets/{tk['id']}/waits", actor="a1", reason="要日志", at="2026-01-01T09:30:00Z")
    clock(client, "2026-01-01T11:00:00Z")
    d = get(client, tk["id"])
    assert d["resolution"]["waiting"] is True
    # 09:00-11:00 = 120 分钟，扣除 09:30-11:00 = 90 分钟等待 => 30
    assert d["resolution"]["elapsed_min"] == 30
    assert d["resolution"]["paused_min"] == 90
    # 响应不受暂停影响
    assert d["response"]["elapsed_min"] == 10


def test_note_does_not_end_wait_but_customer_reply_does(client):
    t = team(client, "A", rb=60, sb=120)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T09:30:00Z")
    post(client, f"/tickets/{tk['id']}/waits", actor="a1", at="2026-01-01T09:30:00Z")
    # 普通留言 / 客服答复
    clock(client, "2026-01-01T09:40:00Z")
    post(client, f"/tickets/{tk['id']}/notes", actor="a2", content="客户还没回", at="2026-01-01T09:40:00Z")
    clock(client, "2026-01-01T09:45:00Z")
    post(client, f"/tickets/{tk['id']}/replies", actor="a2", useful=True, at="2026-01-01T09:45:00Z")
    clock(client, "2026-01-01T10:00:00Z")
    assert get(client, tk["id"])["resolution"]["waiting"] is True
    # 客户回复结束等待
    post(client, f"/tickets/{tk['id']}/customer-replies", content="日志在此", at="2026-01-01T10:00:00Z")
    clock(client, "2026-01-01T10:30:00Z")
    d = get(client, tk["id"])
    assert d["resolution"]["waiting"] is False
    # 09:00-09:30 = 30，等待 09:30-10:00 = 30 不计，10:00-10:30 = 30 => 60
    assert d["resolution"]["elapsed_min"] == 60
    assert d["resolution"]["paused_min"] == 30


def test_wait_requires_agent_request(client):
    t = team(client, "A", rb=60, sb=120)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    # 客户主动留言不构成暂停
    post(client, f"/tickets/{tk['id']}/customer-replies", content="补充", at="2026-01-01T09:10:00Z")
    clock(client, "2026-01-01T10:00:00Z")
    assert get(client, tk["id"])["resolution"]["paused_min"] == 0


# --------------------------------------------------------------------------
# 转组
# --------------------------------------------------------------------------


def test_transfer_keeps_past_and_continues_on_new_calendar(client):
    a = team(client, "A", rb=60, sb=600)
    b = team(client, "B", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, a["id"], "2026-01-01", [[540, 600]])   # A: 09:00-10:00
    cal(client, b["id"], "2026-01-01", [[600, 660]])   # B: 10:00-11:00
    tk = ticket(client, a["id"])
    clock(client, "2026-01-01T10:00:00Z")
    post(client, f"/tickets/{tk['id']}/transfer", to_team_id=b["id"], at="2026-01-01T10:00:00Z")
    clock(client, "2026-01-01T11:00:00Z")
    v = get(client, tk["id"])["resolution"]
    # 转组不归零：A 段 60 + B 段 60 = 120
    assert v["elapsed_min"] == 120
    assert v["budget_min"] == 600  # 冻结的预算不变


def test_transfer_does_not_reset_elapsed(client):
    a = team(client, "A", rb=30, sb=600)
    b = team(client, "B", rb=30, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, a["id"], "2026-01-01", [[0, 1440]])
    cal(client, b["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, a["id"])
    clock(client, "2026-01-01T09:20:00Z")
    post(client, f"/tickets/{tk['id']}/transfer", to_team_id=b["id"], at="2026-01-01T09:20:00Z")
    clock(client, "2026-01-01T09:40:00Z")
    v = get(client, tk["id"])["response"]
    assert v["elapsed_min"] == 40  # 20 + 20，不归零
    assert v["current"]["breach"] is True


# --------------------------------------------------------------------------
# 日历更正
# --------------------------------------------------------------------------


def test_calendar_correction_flips_historical_breach(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])  # 09:00-11:00, 120 分钟
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    d = get(client, tk["id"])
    assert d["response"]["current"]["breach"] is True
    esc = d["escalations"][0]
    assert esc["status"] == "open"

    # 更正为 09:00-10:00，实际耗时 60 => 不超
    clock(client, "2026-01-01T10:40:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])
    d = get(client, tk["id"])
    assert d["response"]["elapsed_min"] == 60
    assert d["response"]["current"]["breach"] is False
    esc2 = d["escalations"][0]
    assert esc2["id"] == esc["id"]
    assert esc2["status"] == "cancelled"
    # 原事实仍保留
    assert d["response"]["original"]["breach"] is True


def test_cannot_acknowledge_cancelled_escalation(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    esc = get(client, tk["id"])["escalations"][0]
    clock(client, "2026-01-01T10:40:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])
    clock(client, "2026-01-01T10:45:00Z")
    r = post(client, f"/escalations/{esc['id']}/acknowledge", actor="duty1")
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"]


def test_acknowledged_escalation_not_deleted_and_closed_with_marker(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    esc = get(client, tk["id"])["escalations"][0]
    r = post(client, f"/escalations/{esc['id']}/acknowledge", actor="duty1", note="接手")
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"

    clock(client, "2026-01-01T10:40:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])
    d = get(client, tk["id"])
    e = d["escalations"][0]
    assert e["id"] == esc["id"]  # 不可删除
    assert e["acknowledged_by"] == "duty1"
    assert e["status"] == "closed"
    assert e["closure_note"] == "判断已更正"

    # 再签收被拒
    r = post(client, f"/escalations/{esc['id']}/acknowledge", actor="duty2")
    assert r.status_code == 409


def test_correction_updates_current_remaining(client):
    t = team(client, "A", rb=120, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    # 原日历 09:00-10:00，到 10:30 已耗 60，余额 60
    assert get(client, tk["id"])["response"]["remaining_min"] == 60
    # 更正为 09:00-11:00：已耗 90，余额 30
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    assert get(client, tk["id"])["response"]["remaining_min"] == 30


# --------------------------------------------------------------------------
# 再次超时：旧升级不复活，另开可签收记录；连续超时不重复开单
# --------------------------------------------------------------------------


def test_rebreach_creates_new_escalation_without_reviving_old(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    first = get(client, tk["id"])["escalations"][0]

    clock(client, "2026-01-01T10:35:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])  # 60 -> 未超，first 取消
    assert get(client, tk["id"])["escalations"][0]["status"] == "cancelled"

    # 又过去 60 分钟，再次超时（09:00-11:00 = 120 > 60）
    clock(client, "2026-01-01T12:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    clock(client, "2026-01-01T12:05:00Z")
    d = get(client, tk["id"])
    assert len(d["escalations"]) == 2
    old, new = d["escalations"]
    assert old["status"] == "cancelled"
    assert new["status"] == "open" and new["seq"] == 2
    r = post(client, f"/escalations/{new['id']}/acknowledge", actor="duty2")
    assert r.status_code == 200


def test_repeated_breach_does_not_open_duplicates(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    for minute in (70, 80, 90, 120):
        clock(client, f"2026-01-01T{9 + minute // 60:02d}:{minute % 60:02d}:00Z")
        get(client, tk["id"])
    assert len(get(client, tk["id"])["escalations"]) == 1


# --------------------------------------------------------------------------
# 完成与判定
# --------------------------------------------------------------------------


def test_completion_cannot_undo_formed_breach(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:10:00Z")
    get(client, tk["id"])  # 形成超时
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T10:10:00Z")
    v = r.json()["response"]
    assert v["elapsed_min"] == 70
    assert v["current"]["breach"] is True
    assert r.json()["escalations"][0]["status"] == "open"


def test_completion_after_correction_within_budget_flips(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:10:00Z")
    get(client, tk["id"])
    # 更正：只算 09:00-09:05 的工作分钟（05 分钟 < 60）
    clock(client, "2026-01-01T10:15:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 545]])
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T10:15:00Z")
    d = r.json()
    assert d["response"]["elapsed_min"] == 5
    assert d["response"]["current"]["breach"] is False
    assert d["escalations"][0]["status"] == "cancelled"


def test_resolution_completion_judged_by_budget(client):
    t = team(client, "A", rb=10, sb=60)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T09:05:00Z")
    post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T09:05:00Z")
    clock(client, "2026-01-01T10:00:00Z")
    r = post(client, f"/tickets/{tk['id']}/resolve", actor="a1", at="2026-01-01T10:00:00Z")
    d = r.json()
    assert d["resolution"]["completed"] is True
    assert d["resolution"]["elapsed_min"] == 60
    assert d["resolution"]["current"]["breach"] is False  # 恰好用完
    assert d["resolution"]["original"]["breach"] is False


# --------------------------------------------------------------------------
# 并发 / 竞争
# --------------------------------------------------------------------------


def test_concurrent_ack_and_correction_single_winner(client):
    """日历更正与签收竞争：事务串行化，签收不能落在已撤销的升级上。"""
    import threading

    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    esc = get(client, tk["id"])["escalations"][0]
    clock(client, "2026-01-01T10:35:00Z")

    results = {}

    def do_ack():
        results["ack"] = post(client, f"/escalations/{esc['id']}/acknowledge", actor="duty1")

    def do_correct():
        results["cal"] = client.put(
            f"/teams/{t['id']}/calendar/2026-01-01",
            json={"team_id": t["id"], "day": "2026-01-01", "intervals": [[540, 600]]},
        )

    threads = [threading.Thread(target=do_ack), threading.Thread(target=do_correct)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    esc_after = client.get(f"/escalations/{esc['id']}").json()
    if results["ack"].status_code == 200:
        # 签收成功 => 更正后应变为 closed，而不是消失
        assert esc_after["status"] == "closed"
        assert esc_after["closure_note"] == "判断已更正"
    else:
        assert results["ack"].status_code == 409
        assert esc_after["status"] == "cancelled"


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------


def test_interval_validation(client):
    t = team(client, "A")
    clock(client, "2026-01-01T09:00:00Z")
    bad = [
        [[540, 540]],           # 空区间
        [[600, 540]],           # 反向
        [[-1, 60]],             # 越界
        [[0, 1441]],            # 越界
        [[540, 600], [580, 700]],  # 重叠
        [["a", 60]],            # 非整数
    ]
    for intervals in bad:
        r = client.put(
            f"/teams/{t['id']}/calendar/2026-01-01",
            json={"team_id": t["id"], "day": "2026-01-01", "intervals": intervals},
        )
        assert r.status_code == 409, (intervals, r.text)


def test_cannot_correct_future_day(client):
    t = team(client, "A")
    clock(client, "2026-01-01T09:00:00Z")
    r = client.put(
        f"/teams/{t['id']}/calendar/2026-01-02",
        json={"team_id": t["id"], "day": "2026-01-02", "intervals": [[0, 60]]},
    )
    assert r.status_code == 409


def test_event_cannot_be_in_future(client):
    t = team(client, "A")
    clock(client, "2026-01-01T09:00:00Z")
    tk = ticket(client, t["id"])
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T09:30:00Z")
    assert r.status_code == 409


# --------------------------------------------------------------------------
# 登记 vs 受理
# --------------------------------------------------------------------------


def test_registration_freezes_budget_before_acceptance(client):
    t = team(client, "A", rb=30, sb=60)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"], accept=False)
    assert tk["status"] == "new"
    assert tk["response"]["budget_min"] == 30  # 登记即冻结
    assert tk["accepted_at"] is None
    # 未受理时两个计时器都不走
    clock(client, "2026-01-01T12:00:00Z")
    d = get(client, tk["id"])
    assert d["response"]["elapsed_min"] == 0
    assert d["resolution"]["elapsed_min"] == 0

    clock(client, "2026-01-01T13:00:00Z")
    r = post(client, f"/tickets/{tk['id']}/accept", actor="a1", at="2026-01-01T13:00:00Z")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "open"
    assert d["response"]["elapsed_min"] == 0
    clock(client, "2026-01-01T13:20:00Z")
    assert get(client, tk["id"])["response"]["elapsed_min"] == 20


def test_actions_require_acceptance(client):
    t = team(client, "A")
    clock(client, "2026-01-01T09:00:00Z")
    tk = ticket(client, t["id"], accept=False)
    r = post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True)
    assert r.status_code == 409
    r = post(client, f"/tickets/{tk['id']}/waits", actor="a1")
    assert r.status_code == 409


def test_customer_cannot_request_wait(client):
    t = team(client, "A", rb=60, sb=120)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    r = post(client, f"/tickets/{tk['id']}/waits", actor="customer")
    assert r.status_code == 409


# --------------------------------------------------------------------------
# 升级归属与查询区分
# --------------------------------------------------------------------------


def test_escalation_records_responsible_team_at_open(client):
    a = team(client, "A", rb=30, sb=600)
    b = team(client, "B", rb=30, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, a["id"], "2026-01-01", [[0, 1440]])
    cal(client, b["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, a["id"])
    clock(client, "2026-01-01T09:20:00Z")
    post(client, f"/tickets/{tk['id']}/transfer", to_team_id=b["id"], at="2026-01-01T09:20:00Z")
    clock(client, "2026-01-01T09:40:00Z")
    d = get(client, tk["id"])
    esc = d["escalations"][0]
    assert esc["team_id"] == b["id"]  # 形成超时时由 B 负责
    assert d["team_id"] == b["id"]


def test_query_distinguishes_original_current_and_outcome(client):
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 660]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    esc = get(client, tk["id"])["escalations"][0]
    post(client, f"/escalations/{esc['id']}/acknowledge", actor="duty1")
    clock(client, "2026-01-01T10:40:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])
    d = get(client, tk["id"])
    r = d["response"]
    # 原事实：仍然记录当时确实超时
    assert r["original"]["breach"] is True
    assert r["original"]["elapsed_min"] == 90
    # 当前判定：更正后不超时
    assert r["current"]["breach"] is False
    # 升级处理结果：已签收，结案保留说明
    assert r["escalation"]["status"] == "closed"
    assert r["escalation"]["acknowledged_by"] == "duty1"
    assert r["escalation"]["closure_note"] == "判断已更正"


def test_late_correction_cannot_fabricate_new_breach(client):
    """未超时 -> 更正后超时：开新升级；再改回却不应额外重复开单。"""
    t = team(client, "A", rb=60, sb=600)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[540, 600]])  # 60 分钟
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T10:30:00Z")
    assert get(client, tk["id"])["response"]["current"]["breach"] is False

    cal(client, t["id"], "2026-01-01", [[540, 660]])  # 90 分钟 => 超时
    d = get(client, tk["id"])
    assert d["response"]["current"]["breach"] is True
    assert [e["status"] for e in d["escalations"]] == ["open"]

    cal(client, t["id"], "2026-01-01", [[540, 600]])  # 改回未超
    d = get(client, tk["id"])
    assert [e["status"] for e in d["escalations"]] == ["cancelled"]
    assert len(d["escalations"]) == 1


def test_resolution_breach_and_response_breach_independent_escalations(client):
    t = team(client, "A", rb=10, sb=30)
    clock(client, "2026-01-01T09:00:00Z")
    cal(client, t["id"], "2026-01-01", [[0, 1440]])
    tk = ticket(client, t["id"])
    clock(client, "2026-01-01T09:05:00Z")
    post(client, f"/tickets/{tk['id']}/replies", actor="a1", useful=True, at="2026-01-01T09:05:00Z")
    clock(client, "2026-01-01T09:40:00Z")
    d = get(client, tk["id"])
    assert d["response"]["current"]["breach"] is False
    assert d["resolution"]["current"]["breach"] is True
    dims = [(e["dimension"], e["status"]) for e in d["escalations"]]
    assert dims == [("resolution", "open")]
