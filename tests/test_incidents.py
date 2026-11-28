"""共享故障通报端到端测试。

覆盖：
- 故障登记、工单关联/撤销（同一故障同一工单只有一条有效关联）
- 通知开关默认开启、关闭后发布不入名单
- 发布锁定正文与名单：客户去重、后来入组/开启不补发
- 永久跳过：发布后关闭通知（重开不恢复）、发布时关联全部撤销（重关联不恢复）、
  撤掉部分关联不影响投递；已决定交付后撤订/撤关联不停止收尾
- 发送替身幂等：接收前/接收后超时均不漏发、不重复触达；服务重启恢复
- 并行推进、租位接管
- 同故障同客户按进展顺序交付，跨客户互不阻塞
- 四态查询、并发一致性、CLI 入口、与 SLA/升级/回拨域隔离
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, incidents as inc  # noqa: E402
from app.main import create_app  # noqa: E402
from app.sender import StubSender  # noqa: E402


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    app = create_app(path)
    assert isinstance(app.state.sender, StubSender)
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
# 构造辅助
# --------------------------------------------------------------------------


def team(client, name="A"):
    r = client.post("/teams", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ticket(client, team_id, title="t"):
    r = client.post("/tickets", json={"title": title, "team_id": team_id})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def incident(client, title="机房断电", description=""):
    r = client.post("/incidents", json={"title": title, "description": description})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def link(client, incident_id, ticket_id, customer_id):
    r = client.post(f"/incidents/{incident_id}/links",
                    json={"ticket_id": ticket_id, "customer_id": customer_id})
    assert r.status_code == 201, r.text
    return r.json()


def unlink(client, incident_id, ticket_id):
    r = client.post(f"/incidents/{incident_id}/unlinks", json={"ticket_id": ticket_id})
    assert r.status_code == 200, r.text
    return r.json()


def set_notify(client, customer_id, enabled):
    r = client.put(f"/customers/{customer_id}/notification", json={"enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()


def publish(client, incident_id, body="进展"):
    r = client.post(f"/incidents/{incident_id}/updates", json={"body": body})
    assert r.status_code == 201, r.text
    return r.json()


def run(client):
    r = client.post("/delivery/run", json={})
    assert r.status_code == 200, r.text
    return r.json()


def deliveries(client, update_id, status=None):
    url = f"/updates/{update_id}/deliveries"
    if status:
        url += f"?status={status}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def statuses(rows):
    return sorted(d["status"] for d in rows)


def key_of(rows, customer_id):
    return next(d["delivery_key"] for d in rows if d["customer_id"] == customer_id)


# --------------------------------------------------------------------------
# 故障与关联
# --------------------------------------------------------------------------


def test_create_incident_and_default_notification_on(client):
    iid = incident(client, "核心交换机故障")
    r = client.get(f"/incidents/{iid}")
    assert r.status_code == 200
    assert r.json()["title"] == "核心交换机故障"
    # 无设置记录：默认开启
    n = client.get("/customers/cust-1/notification")
    assert n.json()["notify"] is True and n.json()["default_on"] is True


def test_link_and_unlink_lifecycle_and_unique_active(client):
    tid = team(client)
    t1, t2 = ticket(client, tid), ticket(client, tid)
    iid = incident(client)

    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-B")
    # 同一工单在同一故障下只能有一条有效关联
    dup = client.post(f"/incidents/{iid}/links",
                      json={"ticket_id": t1, "customer_id": "cust-A"})
    assert dup.status_code == 409
    # 撤销后可以重新关联（历史行保留）
    unlink(client, iid, t1)
    link(client, iid, t1, "cust-A")
    rows = client.get(f"/incidents/{iid}/links").json()
    active = [r for r in rows if r["active"]]
    assert len(active) == 2
    # 撤销不存在的有效关联 -> 409
    r = client.post(f"/incidents/{iid}/unlinks", json={"ticket_id": 99999})
    assert r.status_code == 409
    # 关联不存在的工单 -> 404
    r = client.post(f"/incidents/{iid}/links",
                    json={"ticket_id": 424242, "customer_id": "cust-X"})
    assert r.status_code == 404


def test_link_unknown_incident_is_404(client):
    tid = team(client)
    t1 = ticket(client, tid)
    r = client.post("/incidents/999/links", json={"ticket_id": t1, "customer_id": "c"})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# 发布：锁定名单、客户去重、默认开启、后来变更不补发
# --------------------------------------------------------------------------


def test_publish_locks_recipient_list_and_dedupes_customer(client):
    tid = team(client)
    t1, t2, t3 = ticket(client, tid), ticket(client, tid), ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-A")   # 同一客户第二张工单
    link(client, iid, t3, "cust-B")

    upd = publish(client, iid, "第一次通报")
    assert upd["recipient_count"] == 2
    rows = deliveries(client, upd["id"])
    assert {d["customer_id"] for d in rows} == {"cust-A", "cust-B"}
    a = next(d for d in rows if d["customer_id"] == "cust-A")
    # 快照包含该客户当时的两条关联
    assert {s["ticket_id"] for s in a["snapshot_links"]} == {t1, t2}
    assert all(d["status"] == "pending" for d in rows)

    # 发布后新入组的客户不补发旧进展
    t4 = ticket(client, tid)
    link(client, iid, t4, "cust-C")
    assert len(deliveries(client, upd["id"])) == 2
    assert {d["customer_id"] for d in deliveries(client, upd["id"])} == {"cust-A", "cust-B"}


def test_disabled_notification_excludes_at_publish_and_reenable_no_backfill(client):
    tid = team(client)
    t1, t2 = ticket(client, tid), ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-B")
    set_notify(client, "cust-B", False)

    upd = publish(client, iid)
    rows = deliveries(client, upd["id"])
    assert [d["customer_id"] for d in rows] == ["cust-A"]

    # 发布后重新开启不会补发
    set_notify(client, "cust-B", True)
    run(client)
    assert {d["customer_id"] for d in deliveries(client, upd["id"])} == {"cust-A"}


def test_published_body_is_locked(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid, "锁定的正文")
    run(client)
    d = deliveries(client, upd["id"])[0]
    assert d["body"] == "锁定的正文"
    assert d["status"] == "delivered"


# --------------------------------------------------------------------------
# 永久跳过规则
# --------------------------------------------------------------------------


def test_unsubscribe_between_publish_and_decision_skips_forever(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)

    set_notify(client, "cust-A", False)
    set_notify(client, "cust-A", True)   # 重新开启也不能恢复
    out = run(client)
    assert out["skipped"]
    rows = deliveries(client, upd["id"])
    d = rows[0]
    assert d["status"] == "skipped"
    assert d["skip_reason"] == "unsubscribed"
    assert d["tainted"] is True
    assert d["decided_at"] is not None and d["delivered_at"] is None
    # 再跑也不会复活
    run(client)
    assert deliveries(client, upd["id"])[0]["status"] == "skipped"
    # 发送端从未触达
    assert client.app.state.sender.touched(d["delivery_key"]) is False


def test_all_publish_time_links_revoked_skips_even_if_relinked(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)

    unlink(client, iid, t1)
    # 重新关联：不在发布快照内，不能恢复本次投递
    link(client, iid, t1, "cust-A")
    run(client)
    d = deliveries(client, upd["id"])[0]
    assert d["status"] == "skipped"
    assert d["skip_reason"] == "no_active_link"
    assert client.app.state.sender.touched(d["delivery_key"]) is False


def test_revoking_some_links_keeps_delivery_alive(client):
    tid = team(client)
    t1, t2 = ticket(client, tid), ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-A")
    upd = publish(client, iid)

    unlink(client, iid, t1)   # 快照内还剩 t2 持续有效
    run(client)
    d = deliveries(client, upd["id"])[0]
    assert d["status"] == "delivered"
    assert client.app.state.sender.touched(d["delivery_key"]) is True


def test_decision_made_then_unsubscribe_and_unlink_do_not_stop_finishing(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    d = deliveries(client, upd["id"])[0]
    # 制造 in_flight：发送端接收后超时，决定已做出、结果待确认
    client.app.state.sender.queue_behavior(d["delivery_key"], "post_timeout")
    out = run(client)
    assert out["claimed"] == [d["id"]]
    assert deliveries(client, upd["id"])[0]["status"] == "in_flight"
    # 已决定交付：撤订 + 撤销关联都不能停止收尾
    set_notify(client, "cust-A", False)
    unlink(client, iid, t1)
    run(client)
    final = deliveries(client, upd["id"])[0]
    assert final["status"] == "delivered"
    # 只触达一次
    receipts = [r for r in client.app.state.sender.all_receipts()
                if r["delivery_key"] == d["delivery_key"]]
    assert len(receipts) == 1


def test_new_ticket_after_publish_cannot_revive_skipped_delivery(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    unlink(client, iid, t1)
    run(client)
    assert deliveries(client, upd["id"])[0]["status"] == "skipped"
    # 新增工单并关联，旧进展仍不补发
    t2 = ticket(client, tid)
    link(client, iid, t2, "cust-A")
    run(client)
    assert deliveries(client, upd["id"])[0]["status"] == "skipped"


# --------------------------------------------------------------------------
# 发送端：超时、幂等、重启恢复、并行推进
# --------------------------------------------------------------------------


def test_pre_receive_timeout_retried_without_duplicate(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    d = deliveries(client, upd["id"])[0]

    client.app.state.sender.queue_behavior(d["delivery_key"], "pre_timeout")
    out1 = run(client)
    assert out1["failed"][0]["reason"] == "timeout"
    assert deliveries(client, upd["id"])[0]["status"] == "in_flight"
    assert client.app.state.sender.touched(d["delivery_key"]) is False

    out2 = run(client)  # 重试成功
    assert out2["delivered"][0]["id"] == d["id"]
    final = deliveries(client, upd["id"])[0]
    assert final["status"] == "delivered"
    assert client.app.state.sender.touched(d["delivery_key"]) is True


def test_post_receive_timeout_confirmation_eventually_succeeds_once(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    d = deliveries(client, upd["id"])[0]

    client.app.state.sender.queue_behavior(d["delivery_key"], "post_timeout")
    run(client)
    assert client.app.state.sender.touched(d["delivery_key"]) is True  # 客户已收到
    assert deliveries(client, upd["id"])[0]["status"] == "in_flight"

    client.app.state.sender.queue_behavior(d["delivery_key"], "post_timeout")
    run(client)  # 幂等重放：不重复触达，但本轮仍超时
    assert deliveries(client, upd["id"])[0]["status"] == "in_flight"
    run(client)  # 收到成功回执
    final = deliveries(client, upd["id"])[0]
    assert final["status"] == "delivered"
    receipts = [r for r in client.app.state.sender.all_receipts()
                if r["delivery_key"] == d["delivery_key"]]
    assert len(receipts) == 1
    attempts = client.get(f"/deliveries/{d['id']}/attempts").json()
    kinds = [a["kind"] for a in attempts]
    assert kinds.count("send_timeout") == 2
    assert kinds.count("send_ok") == 1


def test_timeout_delivery_is_not_re_sent_within_same_cycle(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    d = deliveries(client, upd["id"])[0]
    # 持续超时：第一轮 claim 后超时，租位标记为可重试但同轮不再发送
    client.app.state.sender.stick_behavior(d["delivery_key"], "pre_timeout")
    out = run(client)
    assert out["failed"] and not out["reclaimed"]
    assert deliveries(client, upd["id"])[0]["attempts"] == 1
    # 下一轮才接管重试（仍超时），attempts 递增但客户始终未被触达
    out2 = run(client)
    assert out2["reclaimed"] == [d["id"]]
    assert deliveries(client, upd["id"])[0]["attempts"] == 2
    assert client.app.state.sender.touched(d["delivery_key"]) is False
    # 恢复后下一轮确认送达，仅一张回执
    client.app.state.sender.clear_behaviors(d["delivery_key"])
    run(client)
    assert deliveries(client, upd["id"])[0]["status"] == "delivered"
    assert len([r for r in client.app.state.sender.all_receipts()
                if r["delivery_key"] == d["delivery_key"]]) == 1


def test_parallel_run_cycles_claim_each_delivery_once(client):
    tid = team(client)
    iid = incident(client)
    for n in range(6):
        t = ticket(client, tid, title=f"t{n}")
        link(client, iid, t, f"cust-{n}")
    upd = publish(client, iid)
    assert upd["recipient_count"] == 6

    results, errors = [], []

    def worker():
        try:
            r = client.post("/delivery/run", json={})
            results.append(r.json())
        except Exception as exc:  # pragma: no cover - 并发失败可见
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors

    rows = deliveries(client, upd["id"])
    assert statuses(rows) == ["delivered"] * 6
    keys = [d["delivery_key"] for d in rows]
    # 发送端每个投递键恰好一张回执
    for k in keys:
        assert sum(1 for r in client.app.state.sender.all_receipts()
                   if r["delivery_key"] == k) == 1


def test_restart_resumes_unconfirmed_deliveries_with_file_backed_sender(tmp_path):
    db_path = str(tmp_path / "tickets.db")
    sender_path = str(tmp_path / "sender.db")
    os.environ["NOTIFY_SENDER_DB"] = sender_path
    try:
        app = create_app(db_path)
        c = TestClient(app)
        tid = team(c)
        t1 = ticket(c, tid)
        iid = incident(c)
        link(c, iid, t1, "cust-A")
        upd = publish(c, iid)
        d = deliveries(c, upd["id"])[0]
        app.state.sender.queue_behavior(d["delivery_key"], "pre_timeout")
        run(c)
        assert deliveries(c, upd["id"])[0]["status"] == "in_flight"
        c.close()

        # 模拟服务重启：新应用、新发送替身（回执落在同一文件），未确认投递被接管
        app2 = create_app(db_path)
        c2 = TestClient(app2)
        out = run(c2)
        assert out["reclaimed"] == [d["id"]]
        assert deliveries(c2, upd["id"])[0]["status"] == "delivered"
        assert app2.state.sender.touched(d["delivery_key"]) is True
        c2.close()
    finally:
        os.environ.pop("NOTIFY_SENDER_DB", None)


def test_stale_lease_is_reclaimed_by_another_worker(client):
    tid = team(client)
    t1 = ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    upd = publish(client, iid)
    d = deliveries(client, upd["id"])[0]
    # 手动制造一条租约已过期的 in_flight（模拟崩溃进程）
    path = client.app.state.db_path
    conn = db.connect(path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE incident_deliveries SET status='in_flight', decided_at=1,"
        " lease_owner='dead-worker', lease_until=?, attempts=1 WHERE id=?",
        (int(time.time()) - 100, d["id"]),
    )
    conn.execute("COMMIT")
    conn.close()

    out = run(client)
    assert out["reclaimed"] == [d["id"]]
    assert deliveries(client, upd["id"])[0]["status"] == "delivered"


# --------------------------------------------------------------------------
# 顺序门控：同故障同客户按进展顺序，跨客户不阻塞
# --------------------------------------------------------------------------


def test_per_customer_ordering_without_blocking_other_customers(client):
    tid = team(client)
    t1, t2 = ticket(client, tid), ticket(client, tid)
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-B")

    u1 = publish(client, iid, "第一条")
    u2 = publish(client, iid, "第二条")
    u3 = publish(client, iid, "第三条")

    # 第一条的 A 客户卡住（持续接收前超时，保持 in_flight）
    a1 = key_of(deliveries(client, u1["id"]), "cust-A")
    client.app.state.sender.stick_behavior(a1, "pre_timeout")
    out = run(client)
    assert any(f["reason"] == "timeout" for f in out["failed"])

    # cust-A 的第二、三条必须保持 pending（门控）；cust-B 三条全部送达
    run(client)
    a_status = []
    for u in (u1, u2, u3):
        rows = deliveries(client, u["id"])
        a = next(d for d in rows if d["customer_id"] == "cust-A")
        b = next(d for d in rows if d["customer_id"] == "cust-B")
        a_status.append(a["status"])
        assert b["status"] == "delivered"   # 其他客户不受影响
    assert a_status == ["in_flight", "pending", "pending"]

    # 第一条收尾后，后续按序自动放开
    client.app.state.sender.clear_behaviors(a1)
    run(client)
    run(client)
    for u in (u1, u2, u3):
        a = next(d for d in deliveries(client, u["id"]) if d["customer_id"] == "cust-A")
        assert a["status"] == "delivered"
    # 送达顺序：A 客户 u1 先于 u2 先于 u3
    d1 = next(d for d in deliveries(client, u1["id"]) if d["customer_id"] == "cust-A")
    d2 = next(d for d in deliveries(client, u2["id"]) if d["customer_id"] == "cust-A")
    d3 = next(d for d in deliveries(client, u3["id"]) if d["customer_id"] == "cust-A")
    assert d1["delivered_at"] <= d2["delivered_at"] <= d3["delivered_at"]


def test_different_incidents_do_not_share_gating(client):
    tid = team(client)
    t1 = ticket(client, tid)
    i1, i2 = incident(client, "故障一"), incident(client, "故障二")
    link(client, i1, t1, "cust-A")
    link(client, i2, t1, "cust-A")
    u1 = publish(client, i1)
    u2 = publish(client, i2)
    run(client)
    assert statuses(deliveries(client, u1["id"])) == ["delivered"]
    assert statuses(deliveries(client, u2["id"])) == ["delivered"]


# --------------------------------------------------------------------------
# 四态查询
# --------------------------------------------------------------------------


def test_four_way_status_query(client):
    tid = team(client)
    t1, t2, t3, t4 = (ticket(client, tid) for _ in range(4))
    iid = incident(client)
    link(client, iid, t1, "cust-A")
    link(client, iid, t2, "cust-B")
    link(client, iid, t3, "cust-C")
    link(client, iid, t4, "cust-D")
    upd = publish(client, iid)

    # A: in_flight（持续超时待确认）；B: 撤订跳过；C: 撤关联跳过；D: 正常送达；
    # 另让一条新进展的 E 保持 pending
    a_key = key_of(deliveries(client, upd["id"]), "cust-A")
    client.app.state.sender.stick_behavior(a_key, "pre_timeout")
    set_notify(client, "cust-B", False)
    unlink(client, iid, t3)
    run(client)
    run(client)  # D 送达、A 仍 in_flight（重试后仍超时）

    t5 = ticket(client, tid)
    link(client, iid, t5, "cust-E")
    u2 = publish(client, iid)

    by_status = {s: [d["customer_id"] for d in deliveries(client, upd["id"], status=s)]
                 for s in inc.ALL_STATUSES}
    assert by_status["pending"] == []
    assert by_status["skipped"] == ["cust-B", "cust-C"]
    assert by_status["in_flight"] == ["cust-A"]
    assert by_status["delivered"] == ["cust-D"]

    # 第二条进展：E 新入名单；A 因第一条未终结被门控在 pending；D 待下一轮处理
    pending = deliveries(client, u2["id"], status="pending")
    pending_customers = {d["customer_id"] for d in pending}
    assert "cust-E" in pending_customers
    assert "cust-A" in pending_customers        # 顺序门控
    run(client)
    # 再跑一轮：D 送达；A 仍被门控（第一条还卡着）；E 也送达
    u2_rows = deliveries(client, u2["id"])
    assert {d["customer_id"]: d["status"] for d in u2_rows}["cust-D"] == "delivered"
    assert {d["customer_id"]: d["status"] for d in u2_rows}["cust-E"] == "delivered"
    assert {d["customer_id"]: d["status"] for d in u2_rows}["cust-A"] == "pending"
    # 汇总计数（第二条进展：A 仍 pending，D/E 已送达）
    detail = client.get(f"/updates/{u2['id']}").json()
    assert detail["status_counts"] == {"pending": 1, "skipped": 0, "in_flight": 0,
                                       "delivered": 2}
    # 非法状态 -> 409
    assert client.get(f"/updates/{upd['id']}/deliveries?status=bogus").status_code == 409


# --------------------------------------------------------------------------
# 并发：发布 / 撤订 / 撤关联 / 确认交付竞争，结果对应一致顺序
# --------------------------------------------------------------------------


def test_concurrent_mixed_operations_are_consistent(client):
    tid = team(client)
    tickets = [ticket(client, tid, title=f"t{n}") for n in range(5)]
    iid = incident(client)
    for n, t in enumerate(tickets):
        link(client, iid, t, f"cust-{n}")

    errors = []

    def publish_worker():
        try:
            for k in range(3):
                client.post(f"/incidents/{iid}/updates", json={"body": f"通报{k}"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def churn_worker():
        try:
            for k in range(5):
                client.put(f"/customers/cust-{k % 5}/notification",
                           json={"enabled": k % 2 == 0})
                client.post(f"/incidents/{iid}/unlinks", json={"ticket_id": tickets[k % 5]})
                client.post(f"/incidents/{iid}/links",
                            json={"ticket_id": tickets[k % 5], "customer_id": f"cust-{k % 5}"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def run_delivery_worker():
        try:
            for _ in range(5):
                client.post("/delivery/run", json={})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=publish_worker),
               threading.Thread(target=churn_worker),
               threading.Thread(target=run_delivery_worker)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors

    # 不变量核对：每条投递唯一、状态合法、发送端每个键最多一张回执
    path = client.app.state.db_path
    conn = db.connect(path)
    rows = conn.execute("SELECT * FROM incident_deliveries ORDER BY id").fetchall()
    keys = [r["delivery_key"] for r in rows]
    assert len(keys) == len(set(keys))
    for r in rows:
        assert r["status"] in inc.ALL_STATUSES
        if r["status"] == "delivered":
            assert r["delivered_at"] is not None
        if r["status"] == "skipped":
            assert r["skip_reason"] in ("unsubscribed", "no_active_link")
    receipts = client.app.state.sender.all_receipts()
    rkeys = [r["delivery_key"] for r in receipts]
    assert len(rkeys) == len(set(rkeys))
    # 所有有回执的键必然是已决定交付的投递
    decided = {r["delivery_key"] for r in rows if r["status"] in ("in_flight", "delivered")}
    assert set(rkeys) <= decided
    conn.close()


# --------------------------------------------------------------------------
# 与既有 SLA / 升级 / 回拨域隔离
# --------------------------------------------------------------------------


def test_bulletins_do_not_change_ticket_sla_or_events(client):
    tid = team(client)
    t1 = ticket(client, tid)
    before = client.get(f"/tickets/{t1}").json()
    events_before = client.get(f"/tickets/{t1}/events").json()

    iid = incident(client)
    link(client, iid, t1, "cust-A")
    publish(client, iid, "故障通报正文")
    run(client)
    set_notify(client, "cust-A", False)
    publish(client, iid, "第二条")
    unlink(client, iid, t1)

    after = client.get(f"/tickets/{t1}").json()
    events_after = client.get(f"/tickets/{t1}/events").json()
    # 工单状态、SLA 判定、行为流水完全不变（通报不是答复/结案）
    assert after["status"] == before["status"]
    assert after["response"] == before["response"]
    assert after["resolution"] == before["resolution"]
    assert after["escalations"] == before["escalations"]
    assert events_after == events_before


# --------------------------------------------------------------------------
# CLI 入口
# --------------------------------------------------------------------------


def test_cli_run_once(tmp_path):
    db_path = str(tmp_path / "tickets.db")
    sender_path = str(tmp_path / "sender.db")
    env = dict(os.environ)
    env["TICKET_DB"] = db_path
    env["NOTIFY_SENDER_DB"] = sender_path
    env["PYTHONPATH"] = "/workspace"

    # 先通过应用准备数据
    app = create_app(db_path)
    c = TestClient(app)
    tid = team(c)
    t1 = ticket(c, tid)
    iid = incident(c)
    link(c, iid, t1, "cust-A")
    upd = publish(c, iid)
    c.close()

    proc = subprocess.run(
        [sys.executable, "-m", "app.delivery_worker", "--db", db_path],
        cwd="/workspace", env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["delivered"] and not payload["skipped"]

    app2 = create_app(db_path)
    c2 = TestClient(app2)
    assert deliveries(c2, upd["id"])[0]["status"] == "delivered"
    c2.close()


def test_sender_idempotency_contract_directly(tmp_path):
    s = StubSender(str(tmp_path / "s.db"))
    s.queue_behavior("k1", "post_timeout")
    with pytest.raises(Exception):
        s.send("k1", "cust-1", "正文")
    # 重试：幂等重放，不产生第二张回执
    r1 = s.send("k1", "cust-1", "正文")
    assert r1.duplicate is True
    r2 = s.send("k1", "cust-1", "正文")
    assert r2.duplicate is True
    assert len([x for x in s.all_receipts() if x["delivery_key"] == "k1"]) == 1
    # 新键正常触达一次
    r3 = s.send("k2", "cust-2", "另一封")
    assert r3.duplicate is False
    s.close()
