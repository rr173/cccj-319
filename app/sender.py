"""可替换的通知发送端抽象与本地替身。

发送端契约（真实渠道适配器只需实现同样的接口）：
- 接收稳定的投递标识 ``delivery_key``、客户标识 ``customer_id`` 与正文 ``body``。
- 同一 ``delivery_key`` 重试只会触达客户一次（幂等）。
- 可能在「接收前」或「接收后」超时：调用方拿不到结果，必须允许稍后重试；
  重试时发送端依据持久回执去重，绝不二次触达。

``StubSender`` 是本地可运行的替身：
- 回执持久化在独立 SQLite 文件（也可用 ``:memory:``），服务重启后仍记得已触达键。
- 可用 ``queue_*`` 为指定投递键预置下一次调用的行为（接收前/接收后超时、通用失败）。
- 测试可通过 ``touched`` / ``call_count`` 断言不漏发、不重复触达。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass


class TransportError(Exception):
    """发送端通用失败：未产生持久回执，调用方应重试。"""


class TransportTimeout(TransportError):
    """超时：可能发生在接收前或接收后，调用方拿不到确定结果，应重试。"""


@dataclass(frozen=True)
class SendResult:
    delivery_key: str
    customer_id: str
    duplicate: bool          # True 表示此前已触达，本次为幂等重放，未再次触达客户


class Sender:
    """发送端接口。替换真实渠道时实现这三个方法即可。"""

    def send(self, delivery_key: str, customer_id: str, body: str) -> SendResult:
        raise NotImplementedError

    def has_receipt(self, delivery_key: str) -> bool:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 默认无资源
        pass


class StubSender(Sender):
    """本地发送替身：持久回执 + 按投递键配置的故障注入。

    每个键的行为队列按调用次序消费；队列耗尽后默认成功。
    可能的行为：
      - "pre_timeout"  : 接收前超时，不写回执，抛 TransportTimeout
      - "post_timeout" : 接收后超时，**先持久化回执**再抛 TransportTimeout
      - "error"        : 通用失败，不写回执，抛 TransportError
      - "ok" / 缺省    : 持久化回执并返回成功
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        # 并行 worker 跨线程调用：关闭同线程限制，所有访问由 self._lock 串行化。
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sender_receipts ("
            " delivery_key TEXT PRIMARY KEY, customer_id TEXT NOT NULL,"
            " body TEXT NOT NULL, at REAL NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sender_behaviors ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_key TEXT NOT NULL,"
            " behavior TEXT NOT NULL, repeatable INTEGER NOT NULL DEFAULT 0)"
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 故障注入

    def queue_behavior(self, delivery_key: str, behavior: str) -> None:
        """为指定键排队一次行为（按调用次序消费，耗尽后默认成功）。"""
        self._add_behavior(delivery_key, behavior, repeatable=0)

    def stick_behavior(self, delivery_key: str, behavior: str) -> None:
        """为指定键设置持续生效的行为（如一直超时），直到 clear_behaviors。"""
        self._add_behavior(delivery_key, behavior, repeatable=1)

    def _add_behavior(self, delivery_key: str, behavior: str, repeatable: int) -> None:
        if behavior not in ("ok", "pre_timeout", "post_timeout", "error"):
            raise ValueError(f"未知发送行为 {behavior}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO sender_behaviors (delivery_key, behavior, repeatable)"
                " VALUES (?, ?, ?)",
                (delivery_key, behavior, repeatable),
            )

    def clear_behaviors(self, delivery_key: str | None = None) -> None:
        with self._lock:
            if delivery_key is None:
                self._conn.execute("DELETE FROM sender_behaviors")
            else:
                self._conn.execute(
                    "DELETE FROM sender_behaviors WHERE delivery_key = ?", (delivery_key,)
                )

    def _next_behavior(self, delivery_key: str) -> str:
        row = self._conn.execute(
            "SELECT id, behavior, repeatable FROM sender_behaviors"
            " WHERE delivery_key = ? ORDER BY id LIMIT 1",
            (delivery_key,),
        ).fetchone()
        if row is None:
            return "ok"
        if not row[2]:
            self._conn.execute("DELETE FROM sender_behaviors WHERE id = ?", (row[0],))
        return row[1]

    # ------------------------------------------------------------------ 发送

    def send(self, delivery_key: str, customer_id: str, body: str) -> SendResult:
        if not isinstance(delivery_key, str) or not delivery_key:
            raise ValueError("delivery_key 必须是非空字符串")
        with self._lock:
            behavior = self._next_behavior(delivery_key)
            if behavior == "pre_timeout":
                # 接收前超时：回执不存在，重试会真正触达
                raise TransportTimeout(f"接收前超时: {delivery_key}")
            if behavior == "post_timeout":
                # 接收后超时：客户已触达，先落回执再超时；重试只能拿到幂等重放
                self._touch(delivery_key, customer_id, body)
                raise TransportTimeout(f"接收后超时: {delivery_key}")
            if behavior == "error":
                raise TransportError(f"发送失败: {delivery_key}")
            duplicate = self._touch(delivery_key, customer_id, body)
            return SendResult(delivery_key=delivery_key, customer_id=customer_id, duplicate=duplicate)

    def _touch(self, delivery_key: str, customer_id: str, body: str) -> bool:
        """幂等触达：返回 True 表示此前已有回执（本次未重复触达客户）。"""
        import time as _time

        existing = self._conn.execute(
            "SELECT 1 FROM sender_receipts WHERE delivery_key = ?", (delivery_key,)
        ).fetchone()
        if existing is not None:
            return True
        self._conn.execute(
            "INSERT INTO sender_receipts (delivery_key, customer_id, body, at) VALUES (?, ?, ?, ?)",
            (delivery_key, customer_id, body, _time.time()),
        )
        return False

    def has_receipt(self, delivery_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sender_receipts WHERE delivery_key = ?", (delivery_key,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 观察/测试

    def touched(self, delivery_key: str) -> bool:
        return self.has_receipt(delivery_key)

    def call_count(self, delivery_key: str) -> int:
        """该键被 send 的累计次数（含超时与失败尝试），由投递侧 attempts 间接可查；
        这里直接数回执表无法体现失败调用，测试改用 deliveries.attempts。"""
        raise NotImplementedError("使用 deliveries.attempts 或 has_receipt 观察")

    def all_receipts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT delivery_key, customer_id, body, at FROM sender_receipts ORDER BY at, delivery_key"
            ).fetchall()
        return [
            {"delivery_key": r[0], "customer_id": r[1], "body": r[2], "at": r[3]} for r in rows
        ]

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sender_receipts")
            self._conn.execute("DELETE FROM sender_behaviors")

    def close(self) -> None:
        self._conn.close()


def new_delivery_key() -> str:
    """生成全局唯一且稳定的投递标识。"""
    return "dlv_" + uuid.uuid4().hex


def build_sender() -> Sender:
    """按环境变量构造发送端；默认内存替身，生产替换点在此。

    - ``NOTIFY_SENDER=stub``（默认）：本地替身，回执库取 ``NOTIFY_SENDER_DB``，
      缺省为内存库（服务重启不保留；给一个文件路径即可跨重启保留回执）。
    - 接入真实渠道时在这里返回对应适配器，业务代码无需改动。
    """
    kind = os.environ.get("NOTIFY_SENDER", "stub")
    if kind != "stub":  # pragma: no cover - 真实渠道接入点
        raise RuntimeError(f"未实现的发送端类型: {kind}")
    return StubSender(os.environ.get("NOTIFY_SENDER_DB", ":memory:"))
