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

-- --------------------------------------------------------------------------
-- 回拨预约：坐席档案 / 技能 / 可用时段 / 预约
-- 可用时段与团队时限日历相互独立，使用绝对 epoch 分钟的半开区间。
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    team_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id)
);
CREATE INDEX IF NOT EXISTS idx_agents_team ON agents(team_id);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id INTEGER NOT NULL,
    skill    TEXT    NOT NULL,
    PRIMARY KEY (agent_id, skill),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);

-- 坐席可用时段：绝对分钟的半开区间 [start_minute, end_minute)，同一坐席互不重叠
CREATE TABLE IF NOT EXISTS agent_availability (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     INTEGER NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute   INTEGER NOT NULL,
    CHECK (start_minute < end_minute),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_availability_agent ON agent_availability(agent_id, start_minute);

-- 回拨预约。status: pending | in_progress | completed | cancelled | missed
CREATE TABLE IF NOT EXISTS callbacks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         INTEGER NOT NULL,
    team_id           INTEGER NOT NULL,   -- 当前承接团队；转组时随未开始预约一并转移
    skill             TEXT    NOT NULL,   -- 所需技能（客户承诺的一部分，转组保留）
    start_minute      INTEGER NOT NULL,   -- 约定窗口 [start, end)
    end_minute        INTEGER NOT NULL,
    status            TEXT    NOT NULL,
    agent_id          INTEGER,            -- 预选坐席；开始后固定为执行坐席
    booked_at         INTEGER NOT NULL,
    reschedule_count  INTEGER NOT NULL DEFAULT 0,
    started_at        INTEGER,
    ended_at          INTEGER,
    cancelled_at      INTEGER,
    cancel_reason     TEXT,
    missed_at         INTEGER,
    CHECK (start_minute < end_minute),
    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);
CREATE INDEX IF NOT EXISTS idx_callbacks_ticket ON callbacks(ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callbacks(status);
CREATE INDEX IF NOT EXISTS idx_callbacks_agent ON callbacks(agent_id);
-- 每张工单同时最多一笔未开始或进行中的回拨
CREATE UNIQUE INDEX IF NOT EXISTS idx_callbacks_one_active
    ON callbacks(ticket_id) WHERE status IN ('pending', 'in_progress');

-- 回拨业务时刻高水位：取时钟登记与回拨操作到达过的最晚分钟，时钟回放不能使其回落
CREATE TABLE IF NOT EXISTS callback_clock (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_minute INTEGER NOT NULL
);

-- --------------------------------------------------------------------------
-- 共享故障通报：故障 / 工单关联 / 通知开关 / 进展 / 逐客户投递
-- 通报是独立域：不写工单答复、不改工单状态，SLA、升级与回拨口径完全不变。
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_by  TEXT    NOT NULL DEFAULT 'agent',
    created_at  INTEGER NOT NULL
);

-- 故障-工单关联（带客户标识）。撤销只置 revoked_at，历史行保留；
-- 部分唯一索引保证同一工单在同一故障下同时只有一条有效关联。
CREATE TABLE IF NOT EXISTS incident_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    ticket_id   INTEGER NOT NULL,
    customer_id TEXT    NOT NULL,
    linked_by   TEXT    NOT NULL DEFAULT 'agent',
    linked_at   INTEGER NOT NULL,
    revoked_by  TEXT,
    revoked_at  INTEGER,
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_links_one_active
    ON incident_links(incident_id, ticket_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_incident_links_customer
    ON incident_links(incident_id, customer_id) WHERE revoked_at IS NULL;

-- 客户通知开关。无记录视为默认开启；显式关闭后 notify=0。
CREATE TABLE IF NOT EXISTS notification_settings (
    customer_id TEXT    PRIMARY KEY,
    notify      INTEGER NOT NULL DEFAULT 1,   -- 1 开启 / 0 关闭
    updated_by  TEXT    NOT NULL DEFAULT 'customer',
    updated_at  INTEGER NOT NULL
);

-- 故障进展：发布即锁定正文与序号；通知名单在发布事务内定格。
CREATE TABLE IF NOT EXISTS incident_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  INTEGER NOT NULL,
    seq          INTEGER NOT NULL,           -- 同一故障内从 1 递增
    body         TEXT    NOT NULL,           -- 锁定的正文
    published_by TEXT    NOT NULL DEFAULT 'agent',
    published_at INTEGER NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_incident_updates_seq
    ON incident_updates(incident_id, seq);

-- 逐客户投递。status: pending | skipped | in_flight | delivered
--   pending   待处理（尚未做出投递/跳过决定，或被同客户更早进展门控）
--   skipped   已永久跳过（skip_reason: unsubscribed | no_active_link）
--   in_flight 已决定交付、已交给发送端但送达结果待确认
--   delivered 已确认送达
-- tainted: 发布之后、决定之前客户曾关闭通知；重新开启也不能消除。
-- lease_*  : 真实时间秒级租位，供并行 worker 抢占，崩溃后超时可被他人接管。
CREATE TABLE IF NOT EXISTS incident_deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_key TEXT    NOT NULL UNIQUE,    -- 稳定投递标识，也是发送端幂等键
    incident_id  INTEGER NOT NULL,
    update_id    INTEGER NOT NULL,
    customer_id  TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    tainted      INTEGER NOT NULL DEFAULT 0,
    skip_reason  TEXT,
    decided_at   INTEGER,                    -- 做出 skipped/in_flight 决定的业务分钟
    delivered_at INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    lease_owner  TEXT,
    lease_until  INTEGER,                    -- 真实 epoch 秒
    created_at   INTEGER NOT NULL,
    FOREIGN KEY (incident_id) REFERENCES incidents(id),
    FOREIGN KEY (update_id) REFERENCES incident_updates(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_update_customer
    ON incident_deliveries(update_id, customer_id);
CREATE INDEX IF NOT EXISTS idx_delivery_customer_order
    ON incident_deliveries(incident_id, customer_id, update_id);
CREATE INDEX IF NOT EXISTS idx_delivery_status ON incident_deliveries(status);

-- 发布时刻的关联快照：跳过判定只认快照内是否仍有持续有效的关联；
-- 发布后重新关联或新增工单都不会出现在这里，因而不能恢复一次投递。
CREATE TABLE IF NOT EXISTS incident_delivery_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    link_id     INTEGER NOT NULL,
    ticket_id   INTEGER NOT NULL,
    FOREIGN KEY (delivery_id) REFERENCES incident_deliveries(id),
    FOREIGN KEY (link_id) REFERENCES incident_links(id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_links_delivery ON incident_delivery_links(delivery_id);

-- 投递尝试流水（含跳过决定与每次发送结果），便于逐客户审计。
CREATE TABLE IF NOT EXISTS incident_delivery_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    kind        TEXT    NOT NULL,            -- skip | send_ok | send_timeout | send_error
    detail      TEXT,
    at          INTEGER NOT NULL,
    FOREIGN KEY (delivery_id) REFERENCES incident_deliveries(id)
);
CREATE INDEX IF NOT EXISTS idx_delivery_attempts_delivery
    ON incident_delivery_attempts(delivery_id, id);
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
