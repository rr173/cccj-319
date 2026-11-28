"""企业客户超时赔付的核销域：账户条款、工单归属、额度、预占、抵扣凭据与冲销。

口径与不变量：
- 资格：仅已结案且按当前口径判为解决超时（resolution_current_breach）的工单
  有资格；额度按结案时刻的 UTC 月份归账，同月按 (结案时刻, 工单编号) 依次
  发放 min(每张赔付额, 当月剩余上限)，剩余不足只给余量。
- 应得是派生值：结案、归属登记、日历更正都会改变它（包括同月其他工单），
  每次相关写操作在同一事务末尾统一 recompute，中间结果不外泄。
- 抵扣凭据：确认时按该笔有效预占当时分得的来源出具，行项目永不改写、
  不改记其他工单；冲销只能整笔进行，释放占用但保留原凭据与冲销结果。
- 填补：某工单当前应得不足以覆盖其未冲销抵扣时，差额由账户内其他工单的
  未抵扣额度按 (结案时刻, 工单编号) 顺序填补（跨月可合用），仍不足记未弥补额；
  填补优先于一切有效预占。
- 预占：必须全额有着落才接受；有效预占按受理顺序分配，来源可随状态重排；
  当前不能全额兑现的一笔立即永久失效且不占额度——失效在写事务内持久化，
  之后额度恢复、他人取消或抵扣冲销都不得复活。
- 幂等：申请编号在账户内唯一并永久绑定原请求；取消/确认/冲销重试只重放
  结果，不重复扣减或释放。同企业同发票同时最多一笔有效预占或未冲销抵扣
  （部分唯一索引兜底 + 事务内校验）。
- 核销是独立域：只读取工单结案时刻与 resolution 当前判定，不改写 SLA、
  升级、回拨与通报的任何行为。
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

from . import service
from . import timeutil

# 申请状态
VALID = "valid"          # 有效预占
CANCELLED = "cancelled"  # 已取消（终态）
INVALID = "invalid"      # 已失效（终态，绝不复活）
CONFIRMED = "confirmed"  # 已确认（已出具抵扣凭据）

# 凭据状态
VOUCHER_ACTIVE = "active"      # 未冲销
VOUCHER_REVERSED = "reversed"  # 已冲销


class Conflict(service.Conflict):
    pass


class NotFound(service.NotFound):
    pass


def _month_of(minute: int) -> str:
    """epoch 分钟 -> 所在 UTC 月份 'YYYY-MM'。"""
    d = _dt.datetime.fromtimestamp(minute * 60, _dt.timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


# --------------------------------------------------------------------------
# 企业账户与固定条款
# --------------------------------------------------------------------------


def create_account(conn, name: str, per_ticket_amount: int, monthly_cap: int,
                   actor: str, now: int) -> dict:
    if per_ticket_amount < 0 or monthly_cap < 0:
        raise Conflict("赔付条款金额不能为负")
    try:
        cur = conn.execute(
            "INSERT INTO credit_accounts (name, per_ticket_amount, monthly_cap, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, per_ticket_amount, monthly_cap, actor, now),
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict("企业账户名称已存在") from exc
    return account_view(get_account(conn, int(cur.lastrowid)))


def get_account(conn, account_id: int) -> dict:
    row = conn.execute("SELECT * FROM credit_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound(f"企业账户 {account_id} 不存在")
    return dict(row)


def account_view(account: dict) -> dict:
    return {
        "id": account["id"],
        "name": account["name"],
        "per_ticket_amount": account["per_ticket_amount"],
        "monthly_cap": account["monthly_cap"],
        "created_by": account["created_by"],
        "created_at": timeutil.iso(account["created_at"]),
    }


def list_accounts(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM credit_accounts ORDER BY id").fetchall()
    return [account_view(dict(r)) for r in rows]


# --------------------------------------------------------------------------
# 工单归属登记
# --------------------------------------------------------------------------


def assign_tickets(conn, account_id: int, ticket_ids: list[int], actor: str, now: int) -> dict:
    """把已有工单一次性归入账户。整批原子：任一工单不存在或已归属则整体拒绝。"""
    get_account(conn, account_id)
    if not ticket_ids:
        raise Conflict("工单编号列表不能为空")
    if len(set(ticket_ids)) != len(ticket_ids):
        raise Conflict("工单编号列表存在重复")
    for tid in ticket_ids:
        row = conn.execute("SELECT id FROM tickets WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise NotFound(f"工单 {tid} 不存在")
    placeholders = ",".join("?" for _ in ticket_ids)
    taken = conn.execute(
        f"SELECT ticket_id, account_id FROM credit_ticket_assignments"
        f" WHERE ticket_id IN ({placeholders})",
        tuple(ticket_ids),
    ).fetchall()
    if taken:
        raise Conflict(
            "部分工单已归入企业账户，不能重复登记",
            detail={
                "conflicts": [
                    {"ticket_id": r["ticket_id"], "account_id": r["account_id"]} for r in taken
                ]
            },
        )
    for tid in ticket_ids:
        conn.execute(
            "INSERT INTO credit_ticket_assignments (account_id, ticket_id, assigned_by, assigned_at)"
            " VALUES (?, ?, ?, ?)",
            (account_id, tid, actor, now),
        )
    recompute_account(conn, account_id)
    return {
        "account_id": account_id,
        "ticket_ids": list(ticket_ids),
        "assigned_at": timeutil.iso(now),
    }


def list_assigned_tickets(conn, account_id: int) -> list[dict]:
    get_account(conn, account_id)
    rows = conn.execute(
        "SELECT * FROM credit_ticket_assignments WHERE account_id = ? ORDER BY id",
        (account_id,),
    ).fetchall()
    return [
        {
            "ticket_id": r["ticket_id"],
            "assigned_by": r["assigned_by"],
            "assigned_at": timeutil.iso(r["assigned_at"]),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# 核心计算：应得 -> 抵扣覆盖/填补 -> 有效预占分配
# --------------------------------------------------------------------------


def _compute_account(conn, account_id: int) -> dict:
    """纯计算当前分配快照，不落库。

    返回：
      tickets:     {ticket_id: {entitled, deducted, own_cover, fill_received,
                                fill_given, unrecovered, reserved, available, ...}}
      order:       池顺序（结案更早优先，同刻按工单编号；未结案排最后）
      allocations: {request_id: [(ticket_id, amount)] | None}；None 表示该笔
                   有效预占当前不能全额兑现，应当失效
      available_total: 可继续预占的余额合计
    """
    account = get_account(conn, account_id)
    rows = conn.execute(
        """SELECT t.id AS ticket_id, t.resolved_at AS resolved_at,
                  t.resolution_current_breach AS breach
           FROM credit_ticket_assignments a
           JOIN tickets t ON t.id = a.ticket_id
           WHERE a.account_id = ?
           ORDER BY t.id""",
        (account_id,),
    ).fetchall()
    tickets: dict[int, dict] = {}
    for r in rows:
        resolved_at = r["resolved_at"]
        breach = bool(int(r["breach"]))
        eligible = resolved_at is not None and breach
        tickets[int(r["ticket_id"])] = {
            "ticket_id": int(r["ticket_id"]),
            "resolved_at": resolved_at,
            "month": _month_of(resolved_at) if resolved_at is not None else None,
            "eligible": eligible,
            "current_breach": breach,
            "entitled": 0,
        }

    # 应得：按结案时刻的 UTC 月份归账，同月按 (结案时刻, 工单编号) 发放，
    # 每张 min(每张赔付额, 当月剩余上限)，剩余不足只给余量。
    by_month: dict[str, list[dict]] = {}
    for t in tickets.values():
        if t["eligible"]:
            by_month.setdefault(t["month"], []).append(t)
    for month_tickets in by_month.values():
        month_tickets.sort(key=lambda t: (t["resolved_at"], t["ticket_id"]))
        remaining = int(account["monthly_cap"])
        for t in month_tickets:
            grant = min(int(account["per_ticket_amount"]), remaining)
            t["entitled"] = grant
            remaining -= grant

    # 未冲销抵扣：按原凭据行归集到工单（凭据永不改记，归属工单不变）
    deducted: dict[int, int] = {}
    for r in conn.execute(
        """SELECT l.ticket_id AS tid, SUM(l.amount) AS total
           FROM deduction_voucher_lines l
           JOIN deduction_vouchers v ON v.id = l.voucher_id
           WHERE v.account_id = ? AND v.status = ?
           GROUP BY l.ticket_id""",
        (account_id, VOUCHER_ACTIVE),
    ):
        deducted[int(r["tid"])] = int(r["total"])
    for tid in deducted:
        if tid not in tickets:  # 防御：归属不可移除，正常不会走到
            tickets[tid] = {
                "ticket_id": tid,
                "resolved_at": None,
                "month": None,
                "eligible": False,
                "current_breach": False,
                "entitled": 0,
            }

    # 池顺序：结案更早优先，同刻按工单编号（未结案额度为 0，排最后无影响）
    def _pool_key(tid: int):
        ra = tickets[tid]["resolved_at"]
        return (ra is None, ra if ra is not None else 0, tid)

    order = sorted(tickets.keys(), key=_pool_key)

    # 抵扣覆盖：先用本工单应得覆盖其未冲销抵扣
    own_cover: dict[int, int] = {}
    short: dict[int, int] = {}
    pool: dict[int, int] = {}
    for tid in order:
        ent = tickets[tid]["entitled"]
        d = deducted.get(tid, 0)
        own_cover[tid] = min(ent, d)
        short[tid] = d - own_cover[tid]
        pool[tid] = ent - own_cover[tid]

    # 填补：差额由账户内其他工单未抵扣额度按池顺序填补（跨月可合用），
    # 仍不足记未弥补额。填补优先于一切有效预占。
    fill_given = {tid: 0 for tid in order}
    fill_received = {tid: 0 for tid in order}
    unrecovered = {tid: 0 for tid in order}
    for s in order:
        need = short[s]
        if need <= 0:
            continue
        for p in order:
            if need == 0:
                break
            avail = pool[p]
            if avail <= 0:
                continue
            take = avail if avail < need else need
            pool[p] -= take
            fill_given[p] += take
            fill_received[s] += take
            need -= take
        unrecovered[s] = need

    # 有效预占按受理顺序分配；当前不能全额兑现的一笔应永久失效且不占额度，
    # 后面的申请仍可用余额。
    reserved = {tid: 0 for tid in order}
    allocations: dict[int, list[tuple[int, int]] | None] = {}
    valid_rows = conn.execute(
        "SELECT id, amount FROM credit_requests WHERE account_id = ? AND status = ? ORDER BY id",
        (account_id, VALID),
    ).fetchall()
    pool_total = sum(pool.values())
    for req in valid_rows:
        amount = int(req["amount"])
        if pool_total >= amount:
            lines: list[tuple[int, int]] = []
            need = amount
            for p in order:
                if need == 0:
                    break
                avail = pool[p]
                if avail <= 0:
                    continue
                take = avail if avail < need else need
                pool[p] -= take
                pool_total -= take
                reserved[p] += take
                lines.append((p, take))
                need -= take
            allocations[int(req["id"])] = lines
        else:
            allocations[int(req["id"])] = None

    per_ticket: dict[int, dict] = {}
    for tid in order:
        t = tickets[tid]
        per_ticket[tid] = {
            **t,
            "deducted": deducted.get(tid, 0),
            "own_cover": own_cover[tid],
            "fill_received": fill_received[tid],
            "fill_given": fill_given[tid],
            "unrecovered": unrecovered[tid],
            "reserved": reserved[tid],
            "available": pool[tid],
        }
    return {
        "account": account,
        "tickets": per_ticket,
        "order": order,
        "allocations": allocations,
        "available_total": sum(pool.values()),
    }


def recompute_account(conn, account_id: int) -> dict:
    """重算账户分配，并把当前不能全额兑现的有效预占持久化为失效（绝不复活）。"""
    snap = _compute_account(conn, account_id)
    now = service.now_minute(conn)
    for req_id, lines in snap["allocations"].items():
        if lines is None:
            conn.execute(
                "UPDATE credit_requests SET status = ?, invalidated_at = ?"
                " WHERE id = ? AND status = ?",
                (INVALID, now, req_id, VALID),
            )
    return snap


def recompute_all(conn) -> None:
    """重算全部账户。结案/日历更正/归属登记等写操作在同事务末尾统一调用，
    保证一次更正影响多张工单时按最终一致状态结算，中间结果不外泄。"""
    rows = conn.execute("SELECT id FROM credit_accounts ORDER BY id").fetchall()
    for r in rows:
        recompute_account(conn, int(r["id"]))


# --------------------------------------------------------------------------
# 预占申请
# --------------------------------------------------------------------------


def get_request_row(conn, request_id: int) -> dict:
    row = conn.execute("SELECT * FROM credit_requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise NotFound(f"预占申请 {request_id} 不存在")
    return dict(row)


def request_view(conn, req: dict, snap: dict | None = None) -> dict:
    view = {
        "id": req["id"],
        "account_id": req["account_id"],
        "invoice_no": req["invoice_no"],
        "application_no": req["application_no"],
        "amount": req["amount"],
        "status": req["status"],
        "created_by": req["created_by"],
        "created_at": timeutil.iso(req["created_at"]),
        "cancelled_at": timeutil.iso(req["cancelled_at"]),
        "invalidated_at": timeutil.iso(req["invalidated_at"]),
        "confirmed_at": timeutil.iso(req["confirmed_at"]),
        "voucher_id": req["voucher_id"],
        "allocations": None,
    }
    if req["status"] == VALID:
        # 有效预占的当前来源（随状态重排；确认时才定格进凭据）
        if snap is None:
            snap = _compute_account(conn, req["account_id"])
        lines = snap["allocations"].get(req["id"]) or []
        view["allocations"] = [{"ticket_id": tid, "amount": amt} for tid, amt in lines]
    return view


def create_request(conn, account_id: int, invoice_no: str, application_no: str,
                   amount: int, actor: str, now: int) -> tuple[dict, bool]:
    """申请预占。返回 (视图, 是否新建)。同一申请编号重试幂等重放原请求。"""
    get_account(conn, account_id)
    if amount <= 0:
        raise Conflict("金额必须为正整数分")
    existing = conn.execute(
        "SELECT * FROM credit_requests WHERE account_id = ? AND application_no = ?",
        (account_id, application_no),
    ).fetchone()
    if existing is not None:
        if existing["invoice_no"] != invoice_no or int(existing["amount"]) != amount:
            raise Conflict(
                "申请编号已绑定其他请求，不能复用",
                detail={"request": request_view(conn, dict(existing))},
            )
        return request_view(conn, dict(existing)), False  # 幂等重放，不重复扣减

    # 同企业同发票：不能同时有两笔有效预占或未冲销抵扣
    clash = conn.execute(
        "SELECT id FROM credit_requests WHERE account_id = ? AND invoice_no = ? AND status = ?",
        (account_id, invoice_no, VALID),
    ).fetchone()
    if clash is not None:
        raise Conflict("同一企业同一发票已存在有效预占")
    clash = conn.execute(
        "SELECT id FROM deduction_vouchers WHERE account_id = ? AND invoice_no = ? AND status = ?",
        (account_id, invoice_no, VOUCHER_ACTIVE),
    ).fetchone()
    if clash is not None:
        raise Conflict("同一企业同一发票已存在未冲销抵扣")

    # 必须全额有着落才接受
    snap = recompute_account(conn, account_id)
    if snap["available_total"] < amount:
        raise Conflict(
            "可预占额度不足，无法全额有着落",
            detail={"available_amount": snap["available_total"]},
        )
    try:
        cur = conn.execute(
            "INSERT INTO credit_requests"
            " (account_id, invoice_no, application_no, amount, status, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, invoice_no, application_no, amount, VALID, actor, now),
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict("同一企业同一发票已存在有效预占") from exc
    req = get_request_row(conn, int(cur.lastrowid))
    return request_view(conn, req, _compute_account(conn, account_id)), True


def list_requests(conn, account_id: int, status: str | None = None) -> list[dict]:
    get_account(conn, account_id)
    snap = recompute_account(conn, account_id)
    sql = "SELECT * FROM credit_requests WHERE account_id = ?"
    args: list = [account_id]
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id"
    return [request_view(conn, dict(r), snap) for r in conn.execute(sql, args).fetchall()]


def get_request(conn, request_id: int) -> dict:
    req = get_request_row(conn, request_id)
    snap = recompute_account(conn, req["account_id"])
    return request_view(conn, get_request_row(conn, request_id), snap)


def cancel_request(conn, request_id: int, actor: str, now: int) -> dict:
    """取消有效预占。重试只重放结果，不重复释放。"""
    req = get_request_row(conn, request_id)
    if req["status"] == CANCELLED:
        return request_view(conn, req)  # 幂等重放
    if req["status"] == CONFIRMED:
        raise Conflict("已确认的抵扣不能取消，只能整笔冲销")
    if req["status"] == INVALID:
        raise Conflict("预占已失效，不能取消")
    conn.execute(
        "UPDATE credit_requests SET status = ?, cancelled_at = ? WHERE id = ? AND status = ?",
        (CANCELLED, now, request_id, VALID),
    )
    # 释放后重排其他有效预占的来源；失效申请不复活
    recompute_account(conn, req["account_id"])
    return request_view(conn, get_request_row(conn, request_id))


def confirm_request(conn, request_id: int, actor: str, now: int) -> dict:
    """确认有效预占，出具不可改写的抵扣凭据（来源 = 该笔当时分得的工单）。"""
    req = get_request_row(conn, request_id)
    if req["status"] == CONFIRMED:
        voucher = conn.execute(
            "SELECT * FROM deduction_vouchers WHERE request_id = ?", (request_id,)
        ).fetchone()
        return voucher_view(conn, dict(voucher))  # 幂等重放，不重复扣减
    if req["status"] == CANCELLED:
        raise Conflict("预占已取消，不能确认")
    if req["status"] == INVALID:
        raise Conflict("预占已失效，不能确认")
    # 先按当前状态统一结算；若该笔已不能全额兑现，会被结算为失效
    snap = recompute_account(conn, req["account_id"])
    req = get_request_row(conn, request_id)
    if req["status"] != VALID:
        raise Conflict("预占当前不能全额兑现，已失效")
    lines = snap["allocations"].get(request_id)
    if lines is None or sum(amt for _, amt in lines) != int(req["amount"]):
        raise Conflict("预占当前不能全额兑现，已失效")  # 防御：不变量被破
    try:
        cur = conn.execute(
            "INSERT INTO deduction_vouchers"
            " (request_id, account_id, invoice_no, amount, status, issued_by, issued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, req["account_id"], req["invoice_no"], req["amount"],
             VOUCHER_ACTIVE, actor, now),
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict("同一企业同一发票已存在未冲销抵扣") from exc
    voucher_id = int(cur.lastrowid)
    for tid, amt in lines:
        conn.execute(
            "INSERT INTO deduction_voucher_lines (voucher_id, ticket_id, amount)"
            " VALUES (?, ?, ?)",
            (voucher_id, tid, amt),
        )
    conn.execute(
        "UPDATE credit_requests SET status = ?, confirmed_at = ?, voucher_id = ? WHERE id = ?",
        (CONFIRMED, now, voucher_id, request_id),
    )
    return voucher_view(conn, get_voucher_row(conn, voucher_id))


# --------------------------------------------------------------------------
# 抵扣凭据与冲销
# --------------------------------------------------------------------------


def get_voucher_row(conn, voucher_id: int) -> dict:
    row = conn.execute("SELECT * FROM deduction_vouchers WHERE id = ?", (voucher_id,)).fetchone()
    if row is None:
        raise NotFound(f"抵扣凭据 {voucher_id} 不存在")
    return dict(row)


def voucher_view(conn, voucher: dict) -> dict:
    req = conn.execute(
        "SELECT application_no FROM credit_requests WHERE id = ?", (voucher["request_id"],)
    ).fetchone()
    lines = conn.execute(
        "SELECT ticket_id, amount FROM deduction_voucher_lines WHERE voucher_id = ? ORDER BY id",
        (voucher["id"],),
    ).fetchall()
    rev = conn.execute(
        "SELECT * FROM deduction_reversals WHERE voucher_id = ?", (voucher["id"],)
    ).fetchone()
    return {
        "id": voucher["id"],
        "request_id": voucher["request_id"],
        "application_no": req["application_no"] if req else None,
        "account_id": voucher["account_id"],
        "invoice_no": voucher["invoice_no"],
        "amount": voucher["amount"],
        "status": voucher["status"],
        "issued_by": voucher["issued_by"],
        "issued_at": timeutil.iso(voucher["issued_at"]),
        "reversed_at": timeutil.iso(voucher["reversed_at"]),
        "lines": [{"ticket_id": r["ticket_id"], "amount": r["amount"]} for r in lines],
        "reversal": None
        if rev is None
        else {
            "id": rev["id"],
            "amount": rev["amount"],
            "actor": rev["actor"],
            "reversed_at": timeutil.iso(rev["reversed_at"]),
        },
    }


def list_vouchers(conn, account_id: int) -> list[dict]:
    get_account(conn, account_id)
    rows = conn.execute(
        "SELECT * FROM deduction_vouchers WHERE account_id = ? ORDER BY id", (account_id,)
    ).fetchall()
    return [voucher_view(conn, dict(r)) for r in rows]


def reverse_voucher(conn, voucher_id: int, actor: str, now: int) -> dict:
    """整笔冲销已确认抵扣：释放占用，保留原凭据与冲销结果。重试不重复释放。"""
    voucher = get_voucher_row(conn, voucher_id)
    if voucher["status"] == VOUCHER_REVERSED:
        return voucher_view(conn, voucher)  # 幂等重放
    conn.execute(
        "UPDATE deduction_vouchers SET status = ?, reversed_at = ? WHERE id = ? AND status = ?",
        (VOUCHER_REVERSED, now, voucher_id, VOUCHER_ACTIVE),
    )
    conn.execute(
        "INSERT INTO deduction_reversals (voucher_id, account_id, amount, actor, reversed_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (voucher_id, voucher["account_id"], voucher["amount"], actor, now),
    )
    # 释放后重排有效预占来源；已失效申请不复活
    recompute_account(conn, voucher["account_id"])
    return voucher_view(conn, get_voucher_row(conn, voucher_id))


# --------------------------------------------------------------------------
# 账户对账
# --------------------------------------------------------------------------


def reconciliation(conn, account_id: int) -> dict:
    """账户对账：各工单当前应得/可继续预占余额/未弥补额，申请状态与凭据。"""
    snap = recompute_account(conn, account_id)
    account = snap["account"]
    tickets_view = []
    for tid in snap["order"]:
        t = snap["tickets"][tid]
        tickets_view.append(
            {
                "ticket_id": tid,
                "resolved_at": timeutil.iso(t["resolved_at"]),
                "month": t["month"],
                "eligible": t["eligible"],
                "current_breach": t["current_breach"],
                "entitled_amount": t["entitled"],
                "deducted_amount": t["deducted"],
                "fill_received_amount": t["fill_received"],
                "fill_given_amount": t["fill_given"],
                "reserved_amount": t["reserved"],
                "available_amount": t["available"],
                "unrecovered_amount": t["unrecovered"],
            }
        )
    totals = {
        key: sum(t[key] for t in tickets_view)
        for key in (
            "entitled_amount",
            "deducted_amount",
            "reserved_amount",
            "available_amount",
            "unrecovered_amount",
        )
    }
    requests = [
        request_view(conn, dict(r), snap)
        for r in conn.execute(
            "SELECT * FROM credit_requests WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
    ]
    vouchers = [
        voucher_view(conn, dict(v))
        for v in conn.execute(
            "SELECT * FROM deduction_vouchers WHERE account_id = ? ORDER BY id", (account_id,)
        ).fetchall()
    ]
    return {
        "account": account_view(account),
        "generated_at": timeutil.iso(service.now_minute(conn)),
        "tickets": tickets_view,
        "totals": totals,
        "requests": requests,
        "vouchers": vouchers,
    }
