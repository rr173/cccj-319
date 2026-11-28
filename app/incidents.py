"""共享故障通报领域：故障登记、工单关联、通知开关、进展发布与投递状态机。

口径与不变量：
- 同一工单在同一故障下只有一条有效关联（部分唯一索引 + 应用校验双保险）。
- 通知默认开启：``notification_settings`` 无记录即视为开启；客户可显式关闭。
- 发布进展即在同一写事务内锁定正文与通知名单：当时有有效关联且开启通知的
  客户才进入名单，同一客户多张工单只生成一条投递；之后入组/开启不补发。
- 投递状态机：pending → skipped | in_flight → delivered。
  * pending 阶段才做跳过决定；两个永久跳过来源：
    - unsubscribed：发布之后到决定之前客户曾关闭通知（tainted，重开也不消除）；
    - no_active_link：发布时快照中的关联已全部撤销（重关联/新增工单不在快照内）。
  * 进入 in_flight 即“已决定交付”，此后只负责重试到确认送达，撤订/撤关联不再影响。
- 同故障同客户严格按进展序号交付：更早一条未终结（pending/in_flight）时，
  后一条保持 pending；某个客户被门控不影响其他客户。
- 全部写操作在 BEGIN IMMEDIATE 事务内执行，SQLite 串行化写者，发布、撤订、
  撤关联与确认交付的并发结果等价于某一个一致的先后顺序。
- 通报是独立域：不写工单答复/结案，不触碰 SLA、升级与回拨的任何表与口径。
"""

from __future__ import annotations

import sqlite3
import time
import uuid

from . import service
from . import timeutil
from .sender import new_delivery_key

# 投递四态：与“按进展查询”的四种口径一一对应
PENDING = "pending"
SKIPPED = "skipped"
IN_FLIGHT = "in_flight"
DELIVERED = "delivered"
TERMINAL_STATUSES = (SKIPPED, DELIVERED)
ALL_STATUSES = (PENDING, SKIPPED, IN_FLIGHT, DELIVERED)

LEASE_SECONDS = 30


class Conflict(service.Conflict):
    pass


class NotFound(service.NotFound):
    pass


# --------------------------------------------------------------------------
# 故障
# --------------------------------------------------------------------------


def create_incident(conn, title: str, description: str, actor: str, now: int) -> dict:
    if not isinstance(title, str) or not title.strip():
        raise Conflict("故障标题不能为空")
    cur = conn.execute(
        "INSERT INTO incidents (title, description, created_by, created_at) VALUES (?, ?, ?, ?)",
        (title.strip(), description or "", actor, now),
    )
    return get_incident(conn, int(cur.lastrowid))


def _incident_row(conn, incident_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        raise NotFound(f"故障 {incident_id} 不存在")
    return row


def get_incident(conn, incident_id: int) -> dict:
    r = _incident_row(conn, incident_id)
    return {
        "id": r["id"],
        "title": r["title"],
        "description": r["description"],
        "created_by": r["created_by"],
        "created_at": timeutil.iso(r["created_at"]),
        "created_at_minute": r["created_at"],
    }


def list_incidents(conn) -> list[dict]:
    rows = conn.execute("SELECT id FROM incidents ORDER BY id").fetchall()
    return [get_incident(conn, int(r["id"])) for r in rows]


# --------------------------------------------------------------------------
# 工单关联（带客户标识）/ 撤销
# --------------------------------------------------------------------------


def link_ticket(conn, incident_id: int, ticket_id: int, customer_id: str,
                actor: str, now: int) -> dict:
    _incident_row(conn, incident_id)
    service._ticket_row(conn, ticket_id)  # 工单必须存在
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise Conflict("客户标识不能为空")
    customer_id = customer_id.strip()

    existing = conn.execute(
        "SELECT id, revoked_at FROM incident_links WHERE incident_id = ? AND ticket_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (incident_id, ticket_id),
    ).fetchone()
    if existing is not None and existing["revoked_at"] is None:
        raise Conflict("该工单已有效关联到此故障")
    try:
        cur = conn.execute(
            "INSERT INTO incident_links (incident_id, ticket_id, customer_id, linked_by, linked_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (incident_id, ticket_id, customer_id, actor, now),
        )
    except sqlite3.IntegrityError as exc:
        # 并发下部分唯一索引兜底
        raise Conflict("该工单已有效关联到此故障") from exc
    return _link_dict(_link_row(conn, int(cur.lastrowid)))


def unlink_ticket(conn, incident_id: int, ticket_id: int, actor: str, now: int) -> dict:
    _incident_row(conn, incident_id)
    row = conn.execute(
        "SELECT * FROM incident_links WHERE incident_id = ? AND ticket_id = ? AND revoked_at IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (incident_id, ticket_id),
    ).fetchone()
    if row is None:
        raise Conflict("该工单当前没有到此故障的有效关联")
    conn.execute(
        "UPDATE incident_links SET revoked_at = ?, revoked_by = ? WHERE id = ?",
        (now, actor, row["id"]),
    )
    return _link_dict(_link_row(conn, int(row["id"])))


def _link_row(conn, link_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM incident_links WHERE id = ?", (link_id,)).fetchone()
    if row is None:  # pragma: no cover - 内部调用，行刚写入/更新
        raise NotFound(f"关联 {link_id} 不存在")
    return row


def _link_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "incident_id": r["incident_id"],
        "ticket_id": r["ticket_id"],
        "customer_id": r["customer_id"],
        "linked_by": r["linked_by"],
        "linked_at": timeutil.iso(r["linked_at"]),
        "revoked_by": r["revoked_by"],
        "revoked_at": timeutil.iso(r["revoked_at"]),
        "active": r["revoked_at"] is None,
    }


def list_links(conn, incident_id: int, *, active_only: bool = False) -> list[dict]:
    _incident_row(conn, incident_id)
    sql = "SELECT * FROM incident_links WHERE incident_id = ?"
    if active_only:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY id"
    return [_link_dict(r) for r in conn.execute(sql, (incident_id,)).fetchall()]


# --------------------------------------------------------------------------
# 客户通知开关（默认开启）
# --------------------------------------------------------------------------


def set_notification(conn, customer_id: str, enabled: bool, actor: str, now: int) -> dict:
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise Conflict("客户标识不能为空")
    customer_id = customer_id.strip()
    conn.execute(
        "INSERT INTO notification_settings (customer_id, notify, updated_by, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(customer_id) DO UPDATE SET notify = excluded.notify,"
        " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (customer_id, 1 if enabled else 0, actor, now),
    )
    if not enabled:
        # 关闭通知“污染”该客户所有尚未决定的投递：决定只在 pending 阶段做，
        # 已 in_flight 的投递已做出交付决定，不受影响。即使随后重新开启，
        # tainted 也不会被清除 → 本次投递永久跳过。
        conn.execute(
            "UPDATE incident_deliveries SET tainted = 1 WHERE customer_id = ? AND status = ?",
            (customer_id, PENDING),
        )
    return get_notification(conn, customer_id)


def get_notification(conn, customer_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM notification_settings WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    enabled = True if row is None else bool(row["notify"])
    return {
        "customer_id": customer_id,
        "notify": enabled,
        "updated_by": None if row is None else row["updated_by"],
        "updated_at": None if row is None else timeutil.iso(row["updated_at"]),
        "default_on": row is None,
    }


# --------------------------------------------------------------------------
# 进展发布：锁定正文与通知名单
# --------------------------------------------------------------------------


def publish_update(conn, incident_id: int, body: str, actor: str, now: int) -> dict:
    _incident_row(conn, incident_id)
    if not isinstance(body, str) or not body.strip():
        raise Conflict("进展正文不能为空")
    body = body.strip()

    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM incident_updates WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()
    seq = int(seq_row["m"]) + 1
    cur = conn.execute(
        "INSERT INTO incident_updates (incident_id, seq, body, published_by, published_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (incident_id, seq, body, actor, now),
    )
    update_id = int(cur.lastrowid)

    # 发布时刻的有效名单：有效关联 + 通知开启（无设置记录即默认开启）。
    # DISTINCT 客户去重：同一客户多张工单只收一份；其全部有效关联进入快照，
    # 供“关联是否已全部撤销”的决定判定。
    rows = conn.execute(
        """
        SELECT DISTINCT l.id AS link_id, l.ticket_id, l.customer_id
        FROM incident_links l
        WHERE l.incident_id = ? AND l.revoked_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM notification_settings n
              WHERE n.customer_id = l.customer_id AND n.notify = 0
          )
        ORDER BY l.customer_id, l.id
        """,
        (incident_id,),
    ).fetchall()

    by_customer: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_customer.setdefault(r["customer_id"], []).append(r)

    delivery_ids = []
    for customer_id, links in by_customer.items():
        dlv = conn.execute(
            """INSERT INTO incident_deliveries
               (delivery_key, incident_id, update_id, customer_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (new_delivery_key(), incident_id, update_id, customer_id, PENDING, now),
        )
        delivery_id = int(dlv.lastrowid)
        conn.executemany(
            "INSERT INTO incident_delivery_links (delivery_id, link_id, ticket_id)"
            " VALUES (?, ?, ?)",
            [(delivery_id, int(l["link_id"]), int(l["ticket_id"])) for l in links],
        )
        delivery_ids.append(delivery_id)

    return get_update(conn, update_id)


def _update_row(conn, update_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM incident_updates WHERE id = ?", (update_id,)).fetchone()
    if row is None:
        raise NotFound(f"进展 {update_id} 不存在")
    return row


def get_update(conn, update_id: int) -> dict:
    r = _update_row(conn, update_id)
    counts = _status_counts(conn, int(r["id"]))
    return {
        "id": r["id"],
        "incident_id": r["incident_id"],
        "seq": r["seq"],
        "body": r["body"],
        "published_by": r["published_by"],
        "published_at": timeutil.iso(r["published_at"]),
        "published_at_minute": r["published_at"],
        "recipient_count": sum(counts.values()),
        "status_counts": counts,
    }


def list_updates(conn, incident_id: int) -> list[dict]:
    _incident_row(conn, incident_id)
    rows = conn.execute(
        "SELECT id FROM incident_updates WHERE incident_id = ? ORDER BY seq", (incident_id,)
    ).fetchall()
    return [get_update(conn, int(r["id"])) for r in rows]


def _status_counts(conn, update_id: int) -> dict[str, int]:
    counts = {s: 0 for s in ALL_STATUSES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM incident_deliveries WHERE update_id = ? GROUP BY status",
        (update_id,),
    ).fetchall()
    for r in rows:
        counts[r["status"]] = int(r["n"])
    return counts


# --------------------------------------------------------------------------
# 投递查询：待处理 / 已跳过 / 已决定交付待确认 / 已确认送达
# --------------------------------------------------------------------------


def _delivery_dict(conn, r: sqlite3.Row) -> dict:
    snapshot = [
        {"link_id": int(x["link_id"]), "ticket_id": int(x["ticket_id"])}
        for x in conn.execute(
            "SELECT link_id, ticket_id FROM incident_delivery_links WHERE delivery_id = ? ORDER BY id",
            (r["id"],),
        ).fetchall()
    ]
    update = conn.execute(
        "SELECT seq, body FROM incident_updates WHERE id = ?", (r["update_id"],)
    ).fetchone()
    return {
        "id": r["id"],
        "delivery_key": r["delivery_key"],
        "incident_id": r["incident_id"],
        "update_id": r["update_id"],
        "update_seq": update["seq"],
        "customer_id": r["customer_id"],
        "body": update["body"],
        # pending=待处理 skipped=已跳过 in_flight=已决定交付但结果待确认 delivered=已确认送达
        "status": r["status"],
        "tainted": bool(r["tainted"]),
        "skip_reason": r["skip_reason"],
        "decided_at": timeutil.iso(r["decided_at"]),
        "delivered_at": timeutil.iso(r["delivered_at"]),
        "attempts": r["attempts"],
        "lease_owner": r["lease_owner"],
        "snapshot_links": snapshot,
        "created_at": timeutil.iso(r["created_at"]),
    }


def get_delivery(conn, delivery_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"投递 {delivery_id} 不存在")
    return _delivery_dict(conn, row)


def list_deliveries(conn, *, incident_id: int | None = None, update_id: int | None = None,
                    customer_id: str | None = None, status: str | None = None) -> list[dict]:
    if status is not None and status not in ALL_STATUSES:
        raise Conflict(f"未知投递状态 {status}")
    if update_id is not None:
        _update_row(conn, update_id)
    if incident_id is not None:
        _incident_row(conn, incident_id)
    sql = "SELECT * FROM incident_deliveries"
    clauses, args = [], []
    if incident_id is not None:
        clauses.append("incident_id = ?")
        args.append(incident_id)
    if update_id is not None:
        clauses.append("update_id = ?")
        args.append(update_id)
    if customer_id is not None:
        clauses.append("customer_id = ?")
        args.append(customer_id)
    if status is not None:
        clauses.append("status = ?")
        args.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY incident_id, customer_id, update_id, id"
    return [_delivery_dict(conn, r) for r in conn.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------
# 投递状态机（全部在调用方提供的 BEGIN IMMEDIATE 事务内执行）
# --------------------------------------------------------------------------


def _record_attempt(conn, delivery_id: int, kind: str, at: int, detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO incident_delivery_attempts (delivery_id, kind, detail, at) VALUES (?, ?, ?, ?)",
        (delivery_id, kind, detail, at),
    )


def list_attempts(conn, delivery_id: int) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM incident_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if exists is None:
        raise NotFound(f"投递 {delivery_id} 不存在")
    rows = conn.execute(
        "SELECT id, kind, detail, at FROM incident_delivery_attempts WHERE delivery_id = ? ORDER BY id",
        (delivery_id,),
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "detail": r["detail"], "at": timeutil.iso(r["at"])}
        for r in rows
    ]


def _snapshot_active_count(conn, delivery_id: int) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM incident_delivery_links dl
        JOIN incident_links l ON l.id = dl.link_id
        WHERE dl.delivery_id = ? AND l.revoked_at IS NULL
        """,
        (delivery_id,),
    ).fetchone()
    return int(row["n"])


def _earlier_unfinished(conn, r: sqlite3.Row) -> bool:
    """同故障同客户是否存在更早一条尚未终结的投递（顺序门控）。"""
    row = conn.execute(
        "SELECT 1 FROM incident_deliveries"
        " WHERE incident_id = ? AND customer_id = ? AND update_id < ?"
        " AND status IN (?, ?) LIMIT 1",
        (r["incident_id"], r["customer_id"], r["update_id"], PENDING, IN_FLIGHT),
    ).fetchone()
    return row is not None


def evaluate_pending(conn, delivery_id: int, now: int) -> str:
    """对一条 pending 投递做决定。返回 blocked / skipped / ready。

    决定只做一次：离开 pending 后不再进入本函数，因此后续撤订/撤关联
    对已决定的投递没有任何影响。
    """
    r = conn.execute(
        "SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if r is None:
        raise NotFound(f"投递 {delivery_id} 不存在")
    if r["status"] != PENDING:
        return "blocked"

    if int(r["tainted"]) == 1:
        _skip(conn, r, "unsubscribed", now)
        return "skipped"
    if _snapshot_active_count(conn, delivery_id) == 0:
        _skip(conn, r, "no_active_link", now)
        return "skipped"
    if _earlier_unfinished(conn, r):
        # 同客户更早一条还没终结；本投递继续等待，不算决定、不写流水。
        return "blocked"
    return "ready"


def _skip(conn, r: sqlite3.Row, reason: str, now: int) -> None:
    conn.execute(
        "UPDATE incident_deliveries SET status = ?, skip_reason = ?, decided_at = ? WHERE id = ?",
        (SKIPPED, reason, now, r["id"]),
    )
    _record_attempt(conn, int(r["id"]), "skip", now, reason)


def claim_ready(conn, delivery_id: int, now: int, owner: str,
                lease_seconds: int = LEASE_SECONDS) -> dict | None:
    """把一条 ready 的 pending 投递原子转为 in_flight 并加租位。

    条件更新保证并行 worker 只有一个能抢占成功；租位用真实时间，
    崩溃后超过租约的 in_flight 可被其他 worker 接管重试。
    返回含正文的投递字典；未抢到（已被他人处理）返回 None。
    """
    r = conn.execute(
        "SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if r is None or r["status"] != PENDING:
        return None
    lease_until = int(time.time()) + lease_seconds
    cur = conn.execute(
        "UPDATE incident_deliveries SET status = ?, decided_at = ?, lease_owner = ?,"
        " lease_until = ?, attempts = attempts + 1 WHERE id = ? AND status = ?",
        (IN_FLIGHT, now, owner, lease_until, delivery_id, PENDING),
    )
    if cur.rowcount == 0:
        return None
    _record_attempt(conn, delivery_id, "claim", now, owner)
    row = conn.execute("SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)).fetchone()
    return _delivery_dict(conn, row)


def reclaim_inflight(conn, now: int, owner: str, lease_seconds: int = LEASE_SECONDS) -> list[dict]:
    """接管可重试的 in_flight 投递，继续重试。

    两类可接管：
    - lease_until 为 0：上一轮发送超时/失败后主动让出，立即可重试；
    - lease_until 已过期：持租 worker 崩溃/重启。
    条件更新保证同一时刻只有一个 worker 接管成功。
    """
    cutoff = int(time.time())
    rows = conn.execute(
        "SELECT * FROM incident_deliveries WHERE status = ?"
        " AND (lease_until IS NULL OR lease_until = 0 OR lease_until < ?) ORDER BY id",
        (IN_FLIGHT, cutoff),
    ).fetchall()
    out = []
    lease_until = int(time.time()) + lease_seconds
    for r in rows:
        cur = conn.execute(
            "UPDATE incident_deliveries SET lease_owner = ?, lease_until = ?, attempts = attempts + 1"
            " WHERE id = ? AND status = ?"
            " AND (lease_until IS NULL OR lease_until = 0 OR lease_until < ?)",
            (owner, lease_until, r["id"], IN_FLIGHT, cutoff),
        )
        if cur.rowcount:
            _record_attempt(conn, int(r["id"]), "reclaim", now, owner)
            out.append(_delivery_dict(
                conn, conn.execute(
                    "SELECT * FROM incident_deliveries WHERE id = ?", (r["id"],)
                ).fetchone()
            ))
    return out


def release_lease(conn, delivery_id: int) -> None:
    """发送结果不确定（超时/失败）：保留 in_flight 决定，把租位标记为立即可重试。

    不回退到 pending（决定已做出，撤订/撤关联永不再影响本投递）；
    lease_until=0 让下一轮 worker 经 reclaim 幂等接管。
    """
    conn.execute(
        "UPDATE incident_deliveries SET lease_owner = NULL, lease_until = 0 WHERE id = ?",
        (delivery_id,),
    )


def record_send_failure(conn, delivery_id: int, now: int, kind: str, detail: str) -> None:
    _record_attempt(conn, delivery_id, kind, now, detail)
    release_lease(conn, delivery_id)


def mark_delivered(conn, delivery_id: int, now: int) -> dict:
    """确认送达。仅 in_flight 可转 delivered；重复确认保持幂等。"""
    r = conn.execute(
        "SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if r is None:
        raise NotFound(f"投递 {delivery_id} 不存在")
    if r["status"] == DELIVERED:
        return _delivery_dict(conn, r)
    if r["status"] != IN_FLIGHT:
        raise Conflict(f"投递当前状态 {r['status']} 不能确认送达")
    conn.execute(
        "UPDATE incident_deliveries SET status = ?, delivered_at = ?,"
        " lease_owner = NULL, lease_until = NULL WHERE id = ?",
        (DELIVERED, now, delivery_id),
    )
    _record_attempt(conn, delivery_id, "send_ok", now, None)
    return _delivery_dict(
        conn, conn.execute("SELECT * FROM incident_deliveries WHERE id = ?", (delivery_id,)).fetchone()
    )


def new_owner_token() -> str:
    return "worker-" + uuid.uuid4().hex
