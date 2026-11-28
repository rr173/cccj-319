"""投递推进器：把 pending 投递推进到 skipped / in_flight / delivered。

设计：
- 每个决定（跳过、抢占、确认）都在独立的 BEGIN IMMEDIATE 短事务内完成，
  与发布、撤订、撤关联等写者在 SQLite 层串行化，结果等价于一致的先后顺序。
- 对发送端的调用在事务**外**进行：发送端可能超时/失败，不持有数据库写锁；
  发送端自身按稳定 delivery_key 幂等，重复重试只触达一次。
- in_flight 带真实时间租位：并行 worker 抢占互斥；进程崩溃后租约过期，
  服务重启或下一轮 worker 自动接管，不漏发、不重复触达。
- 同故障同客户按 update 顺序门控，某客户等待不阻塞其他客户。
- 单轮内每条投递最多调用一次发送端：结果不确定时把租位标记为立即可重试，
  留给下一轮（或并行的其他 worker），避免同轮重复调用。
"""

from __future__ import annotations

import argparse
import os
import sqlite3

from . import db, incidents as inc
from . import service
from .sender import Sender, TransportError, TransportTimeout, build_sender

# 租位默认时长（真实秒）：仅用于崩溃检测；超时/失败让出的投递 lease_until=0 立即可重试
DEFAULT_LEASE_SECONDS = inc.LEASE_SECONDS


def _connect(path: str) -> sqlite3.Connection:
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def run_cycle(conn: sqlite3.Connection, sender: Sender, *,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict:
    """推进一轮，返回各类动作的计数与明细。可被 HTTP 入口、CLI 与测试复用。"""
    owner = inc.new_owner_token()
    result = {"claimed": [], "skipped": [], "blocked": [], "delivered": [],
              "reclaimed": [], "failed": []}

    # 1) 接管可重试的 in_flight（上轮超时/失败让出，或租约过期的崩溃投递）。
    #    条件更新保证并行 worker 中只有一个接管成功。先于 pending 处理，
    #    使同客户更早一条优先收尾，顺序门控尽快放开。
    with db.transaction(conn):
        now = service.now_minute(conn)
        reclaimed = inc.reclaim_inflight(conn, now, owner, lease_seconds)
    for d in reclaimed:
        result["reclaimed"].append(d["id"])
        _send_one(conn, sender, d, result)

    # 2) 逐条评估 pending：跳过 / 被顺序门控 / 抢占交付
    pending_rows = conn.execute(
        "SELECT id FROM incident_deliveries WHERE status = ? ORDER BY id", (inc.PENDING,)
    ).fetchall()
    for row in pending_rows:
        delivery_id = int(row["id"])
        with db.transaction(conn):
            now = service.now_minute(conn)
            verdict = inc.evaluate_pending(conn, delivery_id, now)
            if verdict == "skipped":
                result["skipped"].append(delivery_id)
                continue
            if verdict == "blocked":
                result["blocked"].append(delivery_id)
                continue
            claimed = inc.claim_ready(conn, delivery_id, now, owner, lease_seconds)
        if claimed is None:  # 被并发 worker 抢先处理
            continue
        result["claimed"].append(delivery_id)
        _send_one(conn, sender, claimed, result)

    return result


def _send_one(conn: sqlite3.Connection, sender: Sender, delivery: dict, result: dict) -> None:
    """事务外调用一次发送端并据结果落库；超时/失败保留 in_flight，等待后续重试。"""
    delivery_id = delivery["id"]
    try:
        send_result = sender.send(
            delivery["delivery_key"], delivery["customer_id"], delivery["body"]
        )
    except TransportTimeout as exc:
        # 接收前/接收后超时都可能已触达：不落终态，标记租位立即可重试，稍后幂等重试。
        with db.transaction(conn):
            inc.record_send_failure(
                conn, delivery_id, service.now_minute(conn), "send_timeout", str(exc)
            )
        result["failed"].append({"id": delivery_id, "reason": "timeout"})
        return
    except TransportError as exc:
        with db.transaction(conn):
            inc.record_send_failure(
                conn, delivery_id, service.now_minute(conn), "send_error", str(exc)
            )
        result["failed"].append({"id": delivery_id, "reason": "error"})
        return

    with db.transaction(conn):
        inc.mark_delivered(conn, delivery_id, service.now_minute(conn))
    result["delivered"].append(
        {"id": delivery_id, "duplicate": bool(send_result.duplicate)}
    )


def run_once(db_path: str, sender: Sender | None = None, **kwargs) -> dict:
    """一次性推进入口：CLI 与 HTTP 入口共用。"""
    own_sender = sender is None
    sender = sender or build_sender()
    conn = _connect(db_path)
    try:
        return run_cycle(conn, sender, **kwargs)
    finally:
        conn.close()
        if own_sender:
            sender.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推进故障通报投递（一次性，可反复调用）")
    parser.add_argument("--db", default=os.environ.get("TICKET_DB"),
                        help="工单库路径（默认取 TICKET_DB）")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("必须通过 --db 或环境变量 TICKET_DB 指定数据库路径")

    result = run_once(args.db, lease_seconds=args.lease_seconds)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
