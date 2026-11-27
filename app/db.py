"""SQLite 持久层：建表与连接管理。

时间统一用整数分钟（epoch minutes, UTC）表达，与业务日历的输入口径一致。
所有写操作使用 BEGIN IMMEDIATE，使并发写者在 SQLite 层串行化。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DEFAULT_DB_PATH = os.environ.get(
    "TICKET_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "tickets.db")
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS teams (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT    NOT NULL UNIQUE,
    response_budget_min   INTEGER NOT NULL DEFAULT 60,
    resolution_budget_min INTEGER NOT NULL DEFAULT 480
);

CREATE TABLE IF NOT EXISTS tickets (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    title                       TEXT    NOT NULL,
    status                      TEXT    NOT NULL,          -- open | resolved
    initial_team_id             INTEGER NOT NULL,
    current_team_id             INTEGER NOT NULL,
    created_at                  INTEGER NOT NULL,
    accepted_at                 INTEGER,                   -- 受理时刻; NULL 表示尚未受理
    first_reply_at              INTEGER,                   -- 首次有用答复
    resolved_at                 INTEGER,
    -- 登记时冻结的两个工作分钟预算
    response_budget_min         INTEGER NOT NULL,
    resolution_budget_min       INTEGER NOT NULL,
    -- 原事实（一旦判定即不可变）
    response_original_breach    INTEGER,                   -- 0/1/NULL(尚未判定)
    response_original_elapsed   INTEGER,
    response_original_judged_at INTEGER,
    resolution_original_breach  INTEGER,
    resolution_original_elapsed INTEGER,
    resolution_original_judged_at INTEGER,
    -- 当前判定（随日历更正/时间推进重算）
    response_current_breach     INTEGER NOT NULL DEFAULT 0,
    response_current_judged_at  INTEGER,
    resolution_current_breach   INTEGER NOT NULL DEFAULT 0,
    resolution_current_judged_at INTEGER,
    -- 超时“波次”：同一维度每次由未超时转为超时 +1
    response_breach_episode     INTEGER NOT NULL DEFAULT 0,
    resolution_breach_episode   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (initial_team_id) REFERENCES teams(id),
    FOREIGN KEY (current_team_id) REFERENCES teams(id)
);

-- 工作区间：半开区间 [start_minute, end_minute)，整数分钟，位于某团队日历的某一天
CREATE TABLE IF NOT EXISTS calendar_intervals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id      INTEGER NOT NULL,
    day          TEXT    NOT NULL,   -- YYYY-MM-DD (UTC)
    start_minute INTEGER NOT NULL,   -- 0..1440
    end_minute   INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    CHECK (start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);
CREATE INDEX IF NOT EXISTS idx_intervals_team_day ON calendar_intervals(team_id, day);

CREATE TABLE IF NOT EXISTS calendar_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    before_json TEXT    NOT NULL,
    after_json  TEXT    NOT NULL,
    actor       TEXT    NOT NULL DEFAULT 'duty',
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id    INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    actor        TEXT    NOT NULL DEFAULT 'agent',
    payload_json TEXT    NOT NULL DEFAULT '{}',
    at           INTEGER NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON ticket_events(ticket_id, id);

CREATE TABLE IF NOT EXISTS ticket_transfers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id    INTEGER NOT NULL,
    from_team_id INTEGER NOT NULL,
    to_team_id   INTEGER NOT NULL,
    at           INTEGER NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_transfers_ticket ON ticket_transfers(ticket_id, at, id);

-- 等待客户补资料：由客服发起，只能由关联的客户回复结束
CREATE TABLE IF NOT EXISTS customer_waits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id      INTEGER NOT NULL,
    requested_at   INTEGER NOT NULL,
    requested_by   TEXT    NOT NULL DEFAULT 'agent',
    ended_at       INTEGER,
    ended_by_event INTEGER,
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE INDEX IF NOT EXISTS idx_waits_ticket ON customer_waits(ticket_id, id);

-- 升级记录：每个 (ticket, dimension, seq) 一条；形成后不可删除，也不会复活
CREATE TABLE IF NOT EXISTS escalations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    dimension       TEXT    NOT NULL,   -- response | resolution
    seq             INTEGER NOT NULL,   -- 第几波超时（同一维度内从 1 开始）
    opened_at       INTEGER NOT NULL,
    elapsed_min     INTEGER NOT NULL,
    budget_min      INTEGER NOT NULL,
    -- open | acknowledged | cancelled | closed
    status          TEXT    NOT NULL,
    acknowledged_at INTEGER,
    acknowledged_by TEXT,
    ack_note        TEXT,
    cancelled_at    INTEGER,
    cancel_reason   TEXT,               -- judgment_corrected
    closed_at       INTEGER,
    closure_note    TEXT,               -- judgment_corrected => 「判断已更正」
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_esc_unique ON escalations(ticket_id, dimension, seq);
CREATE INDEX IF NOT EXISTS idx_esc_ticket ON escalations(ticket_id, dimension, seq);

CREATE TABLE IF NOT EXISTS clock_overrides (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,   -- 登记的可控“当前时刻”
    real_at    INTEGER NOT NULL,
    created_by TEXT    NOT NULL DEFAULT 'duty'
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """串行写事务。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
