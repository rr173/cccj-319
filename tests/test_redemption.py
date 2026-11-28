"""企业客户超时赔付核销的端到端测试。

覆盖：账户条款、工单归属、应得额度（UTC 月份归账 + 月度上限按序发放）、
预占/取消/确认/冲销、幂等重试、发票占用、改判后的填补与未弥补额、
预占失效不复活、多工单更正的原子结算、并发与重启持久性。
"""

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


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def team(c, name="A", rb=60, sb=60):
    r = c.post("/teams", json={"name": name, "response_budget_min": rb, "resolution_budget_min": sb})
    assert r.status_code == 201, r.text
    return r.json()


def clock(c, at):
    r = c.post("/clock", json={"at": at})
    assert r.status_code == 200, r.text
    return r.json()


def cal(c, team_id, day, intervals):
    r = c.put(f"/teams/{team_id}/calendar/{day}",
              json={"team_id": team_id, "day": day, "intervals": intervals})
    assert r.status_code == 200, r.text
    return r.json()


def resolved_ticket(c, team_id, accepted_at, resolved_at, sb=60, intervals=None):
    """登记并在指定时刻结案；全天日历下 elapsed = 墙钟分钟。返回工单详情。

    日历只能设定已过/当日，因此在时钟进入对应天后才设定。
    """
    iv = [[0, 1440]] if intervals is None else intervals
    clock(c, accepted_at)
    cal(c, team_id, accepted_at[:10], iv)
    r = c.post("/tickets", json={"title": "t", "team_id": team_id,
                                 "resolution_budget_min": sb, "response_budget_min": 60})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    clock(c, resolved_at)
    if resolved_at[:10] != accepted_at[:10]:
        cal(c, team_id, resolved_at[:10], iv)
    r = c.post(f"/tickets/{tid}/resolve", json={"at": resolved_at})
    assert r.status_code == 200, r.text
    return r.json()


def account(c, name="ACME", per=1000, cap=5000):
    r = c.post("/credit-accounts",
               json={"name": name, "per_ticket_amount": per, "monthly_cap": cap})
    assert r.status_code == 201, r.text
    return r.json()


def assign(c, account_id, ticket_ids):
    r = c.post(f"/credit-accounts/{account_id}/ticket-assignments",
               json={"ticket_ids": ticket_ids})
    assert r.status_code == 201, r.text
    return r.json()


def recon(c, account_id):
    r = c.get(f"/credit-accounts/{account_id}/reconciliation")
    assert r.status_code == 200, r.text
    return r.json()


def ticket_view(recon_view, ticket_id):
    for t in recon_view["tickets"]:
        if t["ticket_id"] == ticket_id:
            return t
    raise AssertionError(f"工单 {ticket_id} 不在对账视图中")


def apply(c, account_id, invoice, app_no, amount):
    return c.post(f"/credit-accounts/{account_id}/credit-requests",
                  json={"invoice_no": invoice, "application_no": app_no, "amount": amount})


# --------------------------------------------------------------------------
# 账户与归属登记
# --------------------------------------------------------------------------


def test_account_registration_and_unique_name(client):
    a = account(client, "ACME", per=1000, cap=5000)
    assert a["per_ticket_amount"] == 1000 and a["monthly_cap"] == 5000
    r = client.post("/credit-accounts",
                    json={"name": "ACME", "per_ticket_amount": 1, "monthly_cap": 1})
    assert r.status_code == 409
    r = client.get("/credit-accounts")
    assert [x["name"] for x in r.json()] == ["ACME"]
    r = client.get("/credit-accounts/999")
    assert r.status_code == 404


def test_assignment_bulk_atomic_and_no_reassign(client):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-01-05T12:00:00Z", "2026-01-05T14:00:00Z")
    a = account(client)
    # 结案后补登记
    assign(client, a["id"], [t1["id"]])
    # 批量原子：t1 已归属 + t3 不存在 => 整体失败，t2 不被写入
    r = client.post(f"/credit-accounts/{a['id']}/ticket-assignments",
                    json={"ticket_ids": [t2["id"], t1["id"], 9999]})
    assert r.status_code in (404, 409)
    rows = client.get(f"/credit-accounts/{a['id']}/tickets").json()
    assert [x["ticket_id"] for x in rows] == [t1["id"]]
    # 不存在的工单 => 404
    r = client.post(f"/credit-accounts/{a['id']}/ticket-assignments",
                    json={"ticket_ids": [9999]})
    assert r.status_code == 404
    # 重复登记同一工单 => 409
    r = client.post(f"/credit-accounts/{a['id']}/ticket-assignments",
                    json={"ticket_ids": [t1["id"]]})
    assert r.status_code == 409
    # 同批重复编号 => 409
    r = client.post(f"/credit-accounts/{a['id']}/ticket-assignments",
                    json={"ticket_ids": [t2["id"], t2["id"]]})
    assert r.status_code == 409


def test_assign_before_resolve_gains_entitlement_on_resolve(client):
    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    clock(client, "2026-01-05T09:00:00Z")
    r = client.post("/tickets", json={"title": "t", "team_id": t["id"],
                                      "resolution_budget_min": 60, "response_budget_min": 60})
    tid = r.json()["id"]
    a = account(client)
    assign(client, a["id"], [tid])  # 结案前登记：尚无资格
    v = recon(client, a["id"])
    tv = ticket_view(v, tid)
    assert tv["eligible"] is False and tv["entitled_amount"] == 0
    clock(client, "2026-01-05T11:00:00Z")
    client.post(f"/tickets/{tid}/resolve", json={"at": "2026-01-05T11:00:00Z"})
    tv = ticket_view(recon(client, a["id"]), tid)
    assert tv["eligible"] is True and tv["entitled_amount"] == 1000


# --------------------------------------------------------------------------
# 应得额度：UTC 月份归账 + 月度上限按序发放
# --------------------------------------------------------------------------


def test_entitlement_monthly_cap_order_and_remainder(client):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-01-06T09:00:00Z", "2026-01-06T11:00:00Z")
    t3 = resolved_ticket(client, t["id"], "2026-01-07T09:00:00Z", "2026-01-07T11:00:00Z")
    a = account(client, per=1000, cap=1500)
    assign(client, a["id"], [t1["id"], t2["id"], t3["id"]])
    v = recon(client, a["id"])
    assert ticket_view(v, t1["id"])["entitled_amount"] == 1000
    assert ticket_view(v, t2["id"])["entitled_amount"] == 500   # 剩余月度额度只给余量
    assert ticket_view(v, t3["id"])["entitled_amount"] == 0
    assert ticket_view(v, t1["id"])["month"] == "2026-01"

    # 更正使 t1 不再超时：同月其他工单应得跟着变化
    cal(client, t["id"], "2026-01-05", [[540, 600]])  # t1 只剩 60 工作分钟
    v = recon(client, a["id"])
    assert ticket_view(v, t1["id"])["entitled_amount"] == 0
    assert ticket_view(v, t2["id"])["entitled_amount"] == 1000
    assert ticket_view(v, t3["id"])["entitled_amount"] == 500


def test_entitlement_same_minute_tiebreak_by_ticket_id(client):
    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    clock(client, "2026-01-05T09:00:00Z")
    r1 = client.post("/tickets", json={"title": "a", "team_id": t["id"],
                                       "resolution_budget_min": 60, "response_budget_min": 60})
    r2 = client.post("/tickets", json={"title": "b", "team_id": t["id"],
                                       "resolution_budget_min": 60, "response_budget_min": 60})
    clock(client, "2026-01-05T11:00:00Z")
    # 同一分钟结案：按工单编号先后发放
    client.post(f"/tickets/{r1.json()['id']}/resolve", json={"at": "2026-01-05T11:00:00Z"})
    client.post(f"/tickets/{r2.json()['id']}/resolve", json={"at": "2026-01-05T11:00:00Z"})
    a = account(client, per=1000, cap=1500)
    assign(client, a["id"], [r2.json()["id"], r1.json()["id"]])  # 登记顺序无关
    v = recon(client, a["id"])
    assert ticket_view(v, r1.json()["id"])["entitled_amount"] == 1000
    assert ticket_view(v, r2.json()["id"])["entitled_amount"] == 500


def test_entitlement_utc_month_boundary(client):
    t = team(client)
    # 1 月 31 日 23:59 结案 -> 1 月账；2 月 1 日 02:00 结案 -> 2 月账
    t1 = resolved_ticket(client, t["id"], "2026-01-31T22:30:00Z", "2026-01-31T23:59:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-02-01T00:00:00Z", "2026-02-01T02:00:00Z")
    a = account(client, per=1000, cap=1000)
    assign(client, a["id"], [t1["id"], t2["id"]])
    v = recon(client, a["id"])
    assert ticket_view(v, t1["id"])["month"] == "2026-01"
    assert ticket_view(v, t2["id"])["month"] == "2026-02"
    assert ticket_view(v, t1["id"])["entitled_amount"] == 1000
    assert ticket_view(v, t2["id"])["entitled_amount"] == 1000  # 跨月各有上限


# --------------------------------------------------------------------------
# 预占：全额有着落、幂等、发票占用
# --------------------------------------------------------------------------


def _two_breached_account(client, per=1000, cap=5000):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-01-06T09:00:00Z", "2026-01-06T11:00:00Z")
    a = account(client, per=per, cap=cap)
    assign(client, a["id"], [t1["id"], t2["id"]])
    return t, a, t1, t2


def test_request_requires_full_coverage(client):
    _, a, t1, t2 = _two_breached_account(client)
    assert apply(client, a["id"], "INV-1", "APP-1", 2001).status_code == 409
    r = apply(client, a["id"], "INV-1", "APP-1", 2000)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "valid"
    assert body["allocations"] == [{"ticket_id": t1["id"], "amount": 1000},
                                   {"ticket_id": t2["id"], "amount": 1000}]
    # 同一申请编号已绑定 2000：改金额重提 => 冲突
    assert apply(client, a["id"], "INV-1", "APP-1", 100).status_code == 409


def test_request_idempotent_replay_binds_original(client):
    _, a, t1, t2 = _two_breached_account(client)
    r = apply(client, a["id"], "INV-1", "APP-1", 1500)
    assert r.status_code == 201
    first = r.json()
    assert first["allocations"] == [{"ticket_id": t1["id"], "amount": 1000},
                                    {"ticket_id": t2["id"], "amount": 500}]
    # 同一申请编号 + 相同参数 => 重放原请求，不重复占用
    r = apply(client, a["id"], "INV-1", "APP-1", 1500)
    assert r.status_code == 200
    assert r.json()["id"] == first["id"]
    v = recon(client, a["id"])
    assert len(v["requests"]) == 1
    assert v["totals"]["reserved_amount"] == 1500
    assert v["totals"]["available_amount"] == 500
    # 同一申请编号 + 不同参数 => 冲突
    assert apply(client, a["id"], "INV-1", "APP-1", 1600).status_code == 409
    assert apply(client, a["id"], "INV-9", "APP-1", 1500).status_code == 409


def test_invoice_slot_rules(client):
    _, a, _, _ = _two_breached_account(client)
    assert apply(client, a["id"], "INV-1", "APP-1", 500).status_code == 201
    # 同企业同发票已有有效预占
    assert apply(client, a["id"], "INV-1", "APP-2", 500).status_code == 409
    # 取消后发票释放
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    r = client.post(f"/credit-requests/{rid}/cancel", json={})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert apply(client, a["id"], "INV-1", "APP-2", 500).status_code == 201
    # 确认后发票被未冲销抵扣占用
    rid2 = client.get(f"/credit-accounts/{a['id']}/credit-requests?status=valid").json()[0]["id"]
    r = client.post(f"/credit-requests/{rid2}/confirm", json={})
    assert r.status_code == 200
    assert apply(client, a["id"], "INV-1", "APP-3", 100).status_code == 409
    # 冲销后发票释放
    vid = r.json()["id"]
    assert client.post(f"/credit-vouchers/{vid}/reverse", json={}).status_code == 200
    assert apply(client, a["id"], "INV-1", "APP-3", 100).status_code == 201


def test_cancel_and_confirm_guards(client):
    _, a, _, _ = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 500)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    # 取消幂等
    assert client.post(f"/credit-requests/{rid}/cancel", json={}).status_code == 200
    r = client.post(f"/credit-requests/{rid}/cancel", json={})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 已取消不能确认
    assert client.post(f"/credit-requests/{rid}/confirm", json={}).status_code == 409

    apply(client, a["id"], "INV-2", "APP-2", 500)
    rid2 = client.get(f"/credit-accounts/{a['id']}/credit-requests?status=valid").json()[0]["id"]
    assert client.post(f"/credit-requests/{rid2}/confirm", json={}).status_code == 200
    # 已确认不能取消，只能冲销
    assert client.post(f"/credit-requests/{rid2}/cancel", json={}).status_code == 409


# --------------------------------------------------------------------------
# 确认凭据与冲销
# --------------------------------------------------------------------------


def test_confirm_issues_immutable_voucher_with_sources(client):
    _, a, t1, t2 = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1500)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    r = client.post(f"/credit-requests/{rid}/confirm", json={})
    assert r.status_code == 200, r.text
    voucher = r.json()
    assert voucher["lines"] == [{"ticket_id": t1["id"], "amount": 1000},
                                {"ticket_id": t2["id"], "amount": 500}]
    assert voucher["status"] == "active" and voucher["reversal"] is None
    # 确认重试 => 同一凭据，不重复扣减
    r = client.post(f"/credit-requests/{rid}/confirm", json={})
    assert r.status_code == 200 and r.json()["id"] == voucher["id"]
    # 申请编号永久绑定：确认后重提仍返回原请求，不新建
    r = apply(client, a["id"], "INV-1", "APP-1", 1500)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert len(recon(client, a["id"])["requests"]) == 1
    v = recon(client, a["id"])
    assert v["totals"]["deducted_amount"] == 1500
    assert v["totals"]["reserved_amount"] == 0
    assert v["totals"]["available_amount"] == 500
    req = [x for x in v["requests"] if x["id"] == rid][0]
    assert req["status"] == "confirmed" and req["voucher_id"] == voucher["id"]


def test_reverse_releases_but_keeps_voucher_and_reversal(client):
    _, a, t1, _ = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    voucher = client.post(f"/credit-requests/{rid}/confirm", json={}).json()
    r = client.post(f"/credit-vouchers/{voucher['id']}/reverse", json={"actor": "fin1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reversed"
    assert body["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]  # 原凭据保留
    assert body["reversal"]["amount"] == 1000 and body["reversal"]["actor"] == "fin1"
    # 冲销重试 => 不重复释放
    r = client.post(f"/credit-vouchers/{voucher['id']}/reverse", json={})
    assert r.status_code == 200 and r.json()["reversal"]["id"] == body["reversal"]["id"]
    v = recon(client, a["id"])
    assert v["totals"]["deducted_amount"] == 0
    assert v["totals"]["available_amount"] == 2000
    assert len(v["vouchers"]) == 1 and v["vouchers"][0]["status"] == "reversed"


# --------------------------------------------------------------------------
# 改判：凭据不改记、填补、未弥补额
# --------------------------------------------------------------------------


def test_rejudgement_keeps_voucher_and_fills_from_other_tickets(client):
    t, a, t1, t2 = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    voucher = client.post(f"/credit-requests/{rid}/confirm", json={}).json()
    assert voucher["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]
    apply(client, a["id"], "INV-2", "APP-2", 1000)  # 分在 t2 上

    # 更正使 t1 不再超时：凭据不改记，差额由 t2 未抵扣额度填补，t2 上的预占失效
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    v = recon(client, a["id"])
    v1 = ticket_view(v, t1["id"])
    assert v1["entitled_amount"] == 0 and v1["deducted_amount"] == 1000
    assert v1["fill_received_amount"] == 1000 and v1["unrecovered_amount"] == 0
    v2 = ticket_view(v, t2["id"])
    assert v2["entitled_amount"] == 1000 and v2["fill_given_amount"] == 1000
    assert v2["reserved_amount"] == 0 and v2["available_amount"] == 0
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-2"]["status"] == "invalid"
    # 原凭据仍按原样对账
    assert v["vouchers"][0]["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]
    assert v["vouchers"][0]["status"] == "active"


def test_unrecovered_amount_and_recovery(client):
    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    a = account(client)
    assign(client, a["id"], [t1["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    client.post(f"/credit-requests/{rid}/confirm", json={})

    # 改判归零：无人能填补 => 未弥补额 1000
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    v = recon(client, a["id"])
    v1 = ticket_view(v, t1["id"])
    assert v1["entitled_amount"] == 0 and v1["deducted_amount"] == 1000
    assert v1["unrecovered_amount"] == 1000
    assert v["totals"]["unrecovered_amount"] == 1000

    # 额度回升：未弥补额被填补回去，凭据始终不变
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    v = recon(client, a["id"])
    v1 = ticket_view(v, t1["id"])
    assert v1["entitled_amount"] == 1000 and v1["unrecovered_amount"] == 0
    assert v["vouchers"][0]["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]


def test_cross_month_fill(client):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-30T09:00:00Z", "2026-01-30T11:00:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-02-02T09:00:00Z", "2026-02-02T11:00:00Z")
    a = account(client, per=1000, cap=1000)
    assign(client, a["id"], [t1["id"], t2["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    client.post(f"/credit-requests/{rid}/confirm", json={})  # 全部来自 t1（结案更早）
    # t1 改判归零：跨月由 2 月的 t2 填补
    cal(client, t["id"], "2026-01-30", [[540, 600]])
    v = recon(client, a["id"])
    assert ticket_view(v, t1["id"])["unrecovered_amount"] == 0
    assert ticket_view(v, t1["id"])["fill_received_amount"] == 1000
    assert ticket_view(v, t2["id"])["fill_given_amount"] == 1000


# --------------------------------------------------------------------------
# 预占失效：立即、永久、不复活；多工单更正原子结算
# --------------------------------------------------------------------------


def test_invalidation_is_immediate_and_never_revived(client):
    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    a = account(client)
    assign(client, a["id"], [t1["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)

    # 改判使额度归零（期间无人查询核销状态）
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    # 额度回升也不能掩盖中间应当失效的预占
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    v = recon(client, a["id"])
    req = v["requests"][0]
    assert req["status"] == "invalid" and req["invalidated_at"] is not None
    assert v["totals"]["reserved_amount"] == 0
    assert v["totals"]["available_amount"] == 1000  # 余额可被后续申请使用
    # 失效是终态：不能确认、不能取消
    assert client.post(f"/credit-requests/{req['id']}/confirm", json={}).status_code == 409
    assert client.post(f"/credit-requests/{req['id']}/cancel", json={}).status_code == 409
    # 后续申请照常
    assert apply(client, a["id"], "INV-2", "APP-2", 1000).status_code == 201


def test_invalidated_request_not_revived_by_reverse(client):
    t, a, t1, t2 = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1500)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    voucher = client.post(f"/credit-requests/{rid}/confirm", json={}).json()
    assert voucher["lines"] == [{"ticket_id": t1["id"], "amount": 1000},
                                {"ticket_id": t2["id"], "amount": 500}]
    apply(client, a["id"], "INV-2", "APP-2", 500)  # t2 余 500
    # t1 改判归零：凭据缺口由 t2 填补，APP-2 无着落而失效
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    v = recon(client, a["id"])
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-2"]["status"] == "invalid"
    assert ticket_view(v, t1["id"])["unrecovered_amount"] == 500
    # 冲销释放全部占用，APP-2 也不复活
    client.post(f"/credit-vouchers/{v['vouchers'][0]['id']}/reverse", json={})
    v = recon(client, a["id"])
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-2"]["status"] == "invalid"
    assert v["totals"]["available_amount"] == 1000  # 仅 t2（t1 已改判不超时）
    assert v["totals"]["unrecovered_amount"] == 0


def test_cancel_does_not_revive_invalidated(client):
    t, a, t1, t2 = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1500)
    apply(client, a["id"], "INV-2", "APP-2", 500)
    # t1 不再超时：总额度 1000，APP-1(1500) 无着落失效，APP-2(500) 仍兑现
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    v = recon(client, a["id"])
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-1"]["status"] == "invalid"
    assert reqs["APP-2"]["status"] == "valid"
    # 取消 APP-2 释放 500，APP-1 也不复活
    client.post(f"/credit-requests/{reqs['APP-2']['id']}/cancel", json={})
    v = recon(client, a["id"])
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-1"]["status"] == "invalid"
    assert v["totals"]["available_amount"] == 1000


def test_later_request_survives_earlier_invalidation(client):
    t, a, t1, t2 = _two_breached_account(client, per=1000, cap=5000)
    apply(client, a["id"], "INV-1", "APP-1", 1500)  # t1:1000 + t2:500
    assert apply(client, a["id"], "INV-2", "APP-2", 600).status_code == 409  # 余额 500 不够
    apply(client, a["id"], "INV-2", "APP-3", 500)
    # t1 改判归零：APP-1 只剩 t2:1000 不够 1500 => 失效；APP-3 仍全额兑现
    cal(client, t["id"], "2026-01-05", [[540, 600]])
    v = recon(client, a["id"])
    reqs = {r["application_no"]: r for r in v["requests"]}
    assert reqs["APP-1"]["status"] == "invalid"
    assert reqs["APP-3"]["status"] == "valid"
    assert reqs["APP-3"]["allocations"] == [{"ticket_id": t2["id"], "amount": 500}]
    assert v["totals"]["reserved_amount"] == 500
    assert v["totals"]["available_amount"] == 500


def test_multi_ticket_correction_settles_atomically(client):
    """一次更正同时翻转多张工单：按最终状态结算，中间结果不使预占失效。"""
    t = team(client)
    # t1: 09:00-11:00 耗 120 -> 超时；t2: 12:00-14:00 耗 0 -> 未超时
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z",
                         intervals=[[0, 720]])
    t2 = resolved_ticket(client, t["id"], "2026-01-05T12:00:00Z", "2026-01-05T14:00:00Z",
                         intervals=[[0, 720]])
    a = account(client)
    assign(client, a["id"], [t1["id"], t2["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)  # 分在 t1
    # 一次更正：t1 变 60 分钟（不超时），t2 变 120 分钟（超时）
    cal(client, t["id"], "2026-01-05", [[600, 840]])  # 10:00-14:00
    v = recon(client, a["id"])
    assert ticket_view(v, t1["id"])["entitled_amount"] == 0
    assert ticket_view(v, t2["id"])["entitled_amount"] == 1000
    req = v["requests"][0]
    assert req["status"] == "valid"  # 不因中间结果失效
    assert req["allocations"] == [{"ticket_id": t2["id"], "amount": 1000}]  # 来源可更换


def test_valid_request_sources_can_shift_before_confirm(client):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    a = account(client)
    assign(client, a["id"], [t1["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    # 补登记一张结案更早的工单：预占来源改用它
    t0 = resolved_ticket(client, t["id"], "2026-01-03T09:00:00Z", "2026-01-03T11:00:00Z")
    assign(client, a["id"], [t0["id"]])
    v = recon(client, a["id"])
    assert v["requests"][0]["allocations"] == [{"ticket_id": t0["id"], "amount": 1000}]
    # 确认以当时分得的来源出具凭据
    rid = v["requests"][0]["id"]
    voucher = client.post(f"/credit-requests/{rid}/confirm", json={}).json()
    assert voucher["lines"] == [{"ticket_id": t0["id"], "amount": 1000}]


def test_assignment_of_earlier_ticket_redistributes_and_fills(client):
    """归属登记改变同月应得：凭据缺口由新工单的未抵扣额度填补。"""
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    t2 = resolved_ticket(client, t["id"], "2026-01-06T09:00:00Z", "2026-01-06T11:00:00Z")
    a = account(client, per=1000, cap=1500)
    assign(client, a["id"], [t1["id"], t2["id"]])
    apply(client, a["id"], "INV-1", "APP-1", 1000)
    rid = client.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
    voucher = client.post(f"/credit-requests/{rid}/confirm", json={}).json()
    assert voucher["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]
    # 补登记结案更早的 t0：月度上限重排 => t0=1000, t1=500, t2=0
    t0 = resolved_ticket(client, t["id"], "2026-01-04T09:00:00Z", "2026-01-04T11:00:00Z")
    assign(client, a["id"], [t0["id"]])
    v = recon(client, a["id"])
    assert ticket_view(v, t0["id"])["entitled_amount"] == 1000
    v1 = ticket_view(v, t1["id"])
    assert v1["entitled_amount"] == 500 and v1["deducted_amount"] == 1000
    assert v1["fill_received_amount"] == 500 and v1["unrecovered_amount"] == 0
    assert ticket_view(v, t0["id"])["fill_given_amount"] == 500
    assert ticket_view(v, t2["id"])["entitled_amount"] == 0
    # 原凭据不改记
    assert v["vouchers"][0]["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]


def test_only_resolution_breach_counts(client):
    """响应超时但解决未超时的工单没有赔付资格。"""
    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    clock(client, "2026-01-05T09:00:00Z")
    r = client.post("/tickets", json={"title": "t", "team_id": t["id"],
                                      "response_budget_min": 30, "resolution_budget_min": 600})
    tid = r.json()["id"]
    clock(client, "2026-01-05T11:00:00Z")
    d = client.post(f"/tickets/{tid}/resolve", json={"at": "2026-01-05T11:00:00Z"}).json()
    assert d["response"]["current"]["breach"] is True     # 响应超时
    assert d["resolution"]["current"]["breach"] is False  # 解决未超时
    a = account(client)
    assign(client, a["id"], [tid])
    tv = ticket_view(recon(client, a["id"]), tid)
    assert tv["eligible"] is False and tv["entitled_amount"] == 0


def test_multiple_vouchers_accumulate_on_same_ticket(client):
    t = team(client)
    t1 = resolved_ticket(client, t["id"], "2026-01-05T09:00:00Z", "2026-01-05T11:00:00Z")
    a = account(client)
    assign(client, a["id"], [t1["id"]])
    for i in (1, 2):
        apply(client, a["id"], f"INV-{i}", f"APP-{i}", 400)
        rid = client.get(f"/credit-accounts/{a['id']}/credit-requests?status=valid").json()[0]["id"]
        client.post(f"/credit-requests/{rid}/confirm", json={})
    v = recon(client, a["id"])
    tv = ticket_view(v, t1["id"])
    assert tv["deducted_amount"] == 800 and tv["available_amount"] == 200
    assert len(v["vouchers"]) == 2


# --------------------------------------------------------------------------
# 对账视图 / 输入校验 / 持久性 / 并发
# --------------------------------------------------------------------------


def test_reconciliation_view_shape(client):
    _, a, t1, t2 = _two_breached_account(client)
    apply(client, a["id"], "INV-1", "APP-1", 1200)
    v = recon(client, a["id"])
    assert v["account"]["name"] == "ACME"
    assert v["totals"] == {
        "entitled_amount": 2000,
        "deducted_amount": 0,
        "reserved_amount": 1200,
        "available_amount": 800,
        "unrecovered_amount": 0,
    }
    assert len(v["tickets"]) == 2 and len(v["requests"]) == 1 and v["vouchers"] == []
    r = client.get(f"/credit-requests/{v['requests'][0]['id']}")
    assert r.status_code == 200 and r.json()["allocations"][0]["ticket_id"] == t1["id"]
    r = client.get(f"/credit-accounts/{a['id']}/credit-requests?status=valid")
    assert len(r.json()) == 1
    r = client.get(f"/credit-accounts/{a['id']}/vouchers")
    assert r.json() == []


def test_input_validation(client):
    _, a, _, _ = _two_breached_account(client)
    assert apply(client, a["id"], "INV-1", "APP-1", 0).status_code == 422
    assert apply(client, a["id"], "INV-1", "APP-1", -5).status_code == 422
    r = client.post(f"/credit-accounts/{a['id']}/credit-requests",
                    json={"invoice_no": "", "application_no": "A", "amount": 1})
    assert r.status_code == 422
    r = client.post("/credit-accounts",
                    json={"name": "X", "per_ticket_amount": -1, "monthly_cap": 0})
    assert r.status_code == 422
    assert apply(client, 9999, "INV-1", "APP-1", 100).status_code == 404
    assert client.get("/credit-requests/9999").status_code == 404
    assert client.get("/credit-vouchers/9999").status_code == 404
    assert client.post("/credit-vouchers/9999/reverse", json={}).status_code == 404


def test_state_survives_restart(client):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    try:
        c1 = TestClient(create_app(path))
        t, a, t1, _ = _two_breached_account(c1)
        apply(c1, a["id"], "INV-1", "APP-1", 1000)
        rid = c1.get(f"/credit-accounts/{a['id']}/credit-requests").json()[0]["id"]
        voucher = c1.post(f"/credit-requests/{rid}/confirm", json={}).json()
        # 改判使另一笔预占失效（落库），随后重启
        apply(c1, a["id"], "INV-2", "APP-2", 500)
        cal(c1, t["id"], "2026-01-05", [[540, 600]])
        cal(c1, t["id"], "2026-01-05", [[0, 1440]])
        before = recon(c1, a["id"])
        c1.close()

        c2 = TestClient(create_app(path))  # 模拟服务重启
        after = recon(c2, a["id"])
        assert after["totals"] == before["totals"]
        reqs = {r["application_no"]: r["status"] for r in after["requests"]}
        assert reqs == {"APP-1": "confirmed", "APP-2": "invalid"}
        assert after["vouchers"][0]["id"] == voucher["id"]
        assert after["vouchers"][0]["lines"] == [{"ticket_id": t1["id"], "amount": 1000}]
        c2.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


def test_concurrent_same_application_no_single_record(client):
    import threading

    _, a, _, _ = _two_breached_account(client)
    results = []

    def do_apply():
        results.append(apply(client, a["id"], "INV-1", "APP-1", 1000))

    threads = [threading.Thread(target=do_apply) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 200, 200, 201]
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1
    v = recon(client, a["id"])
    assert len(v["requests"]) == 1 and v["totals"]["reserved_amount"] == 1000


def test_concurrent_same_invoice_single_winner(client):
    import threading

    _, a, _, _ = _two_breached_account(client)
    results = []

    def do_apply(i):
        results.append(apply(client, a["id"], "INV-1", f"APP-{i}", 1000))

    threads = [threading.Thread(target=do_apply, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409]
    v = recon(client, a["id"])
    assert len(v["requests"]) == 1


def test_concurrent_mixed_mutations_consistent(client):
    """并发结案/更正/归属/核销：最终状态等价于某个一致顺序。"""
    import threading

    t = team(client)
    clock(client, "2026-01-05T08:00:00Z")
    cal(client, t["id"], "2026-01-05", [[0, 1440]])
    clock(client, "2026-01-05T09:00:00Z")
    r = client.post("/tickets", json={"title": "t", "team_id": t["id"],
                                      "resolution_budget_min": 60, "response_budget_min": 60})
    tid = r.json()["id"]
    a = account(client)
    clock(client, "2026-01-05T11:00:00Z")

    def do_resolve():
        client.post(f"/tickets/{tid}/resolve", json={"at": "2026-01-05T11:00:00Z"})

    def do_assign():
        client.post(f"/credit-accounts/{a['id']}/ticket-assignments",
                    json={"ticket_ids": [tid]})

    def do_apply():
        apply(client, a["id"], "INV-1", "APP-1", 1000)

    def do_correct():
        # 更正使已耗变 60：若生效于结案之后，可能使应得归零
        client.put(f"/teams/{t['id']}/calendar/2026-01-05",
                   json={"team_id": t["id"], "day": "2026-01-05", "intervals": [[0, 600]]})

    threads = [threading.Thread(target=f) for f in (do_resolve, do_assign, do_apply, do_correct)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    v = recon(client, a["id"])
    # 不变量：预留 + 可用 + 抵扣占用 - 未弥补 == 应得（无丢失、无重复扣减）
    totals = v["totals"]
    assert totals["reserved_amount"] + totals["available_amount"] \
        + totals["deducted_amount"] - totals["unrecovered_amount"] \
        == totals["entitled_amount"]
    if v["requests"]:
        req = v["requests"][0]
        assert req["status"] in ("valid", "invalid")
        if req["status"] == "valid":
            assert sum(x["amount"] for x in req["allocations"]) == 1000
