"""FastAPI 应用：多团队工单承诺时限。"""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import callbacks as cbmod
from . import db, incidents as incmod, delivery_worker, sender as sendermod, service, timeutil

# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------


class TeamIn(BaseModel):
    name: str = Field(min_length=1)
    response_budget_min: int = Field(default=60, ge=0)
    resolution_budget_min: int = Field(default=480, ge=0)


class TeamBudgetIn(BaseModel):
    response_budget_min: int | None = Field(default=None, ge=0)
    resolution_budget_min: int | None = Field(default=None, ge=0)


class TicketIn(BaseModel):
    title: str = Field(min_length=1)
    team_id: int
    response_budget_min: int | None = Field(default=None, ge=0)
    resolution_budget_min: int | None = Field(default=None, ge=0)
    accept: bool = True          # 登记即受理；false 则先登记，稍后受理
    at: str | None = None
    actor: str = "agent"


class AcceptIn(BaseModel):
    actor: str = "agent"
    at: str | None = None


class CalendarIn(BaseModel):
    team_id: int
    day: str
    intervals: list[Any]
    actor: str = "duty"

    @field_validator("intervals")
    @classmethod
    def _non_empty(cls, v):
        return v


class NoteIn(BaseModel):
    actor: str = "agent"
    content: str = ""
    at: str | None = None


class ReplyIn(BaseModel):
    actor: str = "agent"
    content: str | None = None
    useful: bool = True
    customer: bool = False
    at: str | None = None


class CustomerReplyIn(BaseModel):
    actor: str = "customer"
    content: str | None = None
    at: str | None = None


class WaitIn(BaseModel):
    actor: str = "agent"
    reason: str | None = None
    at: str | None = None


class ResolveIn(BaseModel):
    actor: str = "agent"
    at: str | None = None


class TransferIn(BaseModel):
    to_team_id: int
    actor: str = "agent"
    at: str | None = None


class AckIn(BaseModel):
    actor: str = "agent"
    note: str | None = None


class ClockIn(BaseModel):
    at: str
    actor: str = "duty"


# --------------------------------------------------------------------------
# 回拨预约请求模型
# --------------------------------------------------------------------------


class AgentIn(BaseModel):
    name: str = Field(min_length=1)
    team_id: int
    skills: list[str]
    availability: list[Any]


class AgentPatchIn(BaseModel):
    name: str | None = None
    team_id: int | None = None
    skills: list[str] | None = None
    availability: list[Any] | None = None


class CallbackBookIn(BaseModel):
    skill: str = Field(min_length=1)
    start: Any
    end: Any
    actor: str = "agent"
    agent_id: int | None = None


class CallbackRescheduleIn(BaseModel):
    start: Any
    end: Any
    actor: str = "agent"


class CallbackCancelIn(BaseModel):
    actor: str = "agent"
    reason: str | None = None


class CallbackStartIn(BaseModel):
    agent_id: int
    actor: str = "agent"
    at: str | None = None


class CallbackEndIn(BaseModel):
    actor: str = "agent"
    at: str | None = None


# --------------------------------------------------------------------------
# 共享故障通报请求模型
# --------------------------------------------------------------------------


class IncidentIn(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    actor: str = "agent"


class LinkIn(BaseModel):
    ticket_id: int
    customer_id: str = Field(min_length=1)
    actor: str = "agent"


class UnlinkIn(BaseModel):
    ticket_id: int
    actor: str = "agent"


class NotificationIn(BaseModel):
    enabled: bool
    actor: str = "customer"


class UpdateIn(BaseModel):
    body: str = Field(min_length=1)
    actor: str = "agent"


class DeliveryRunIn(BaseModel):
    lease_seconds: int | None = None


def _at(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return timeutil.parse_iso(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="时间格式应为 ISO-8601，例如 2026-01-01T09:00:00Z") from exc


# --------------------------------------------------------------------------
# 应用工厂
# --------------------------------------------------------------------------


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("TICKET_DB") or os.path.join(tempfile.gettempdir(), "tickets.db")
    bootstrap = db.connect(path)
    db.init_db(bootstrap)
    bootstrap.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="承诺时限后端", version="1.0.0", lifespan=lifespan)
    app.state.db_path = path
    # 可替换发送端：测试可直接替换 app.state.sender；真实渠道在 build_sender 内接入。
    app.state.sender = sendermod.build_sender()

    def get_conn():
        return db.connect(app.state.db_path)

    @app.exception_handler(service.Conflict)
    async def _conflict(request: Request, exc: service.Conflict):
        return JSONResponse(status_code=409, content={"detail": exc.message, **exc.detail})

    @app.exception_handler(service.NotFound)
    async def _not_found(request: Request, exc: service.NotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    # ---------------- 团队与日历 ----------------

    @app.post("/teams", status_code=201)
    def create_team(body: TeamIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                team = service.create_team(
                    conn, body.name, body.response_budget_min, body.resolution_budget_min
                )
            return team
        finally:
            conn.close()

    @app.get("/teams")
    def list_teams():
        conn = get_conn()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM teams ORDER BY id")]
        finally:
            conn.close()

    @app.patch("/teams/{team_id}")
    def patch_team(team_id: int, body: TeamBudgetIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                return service.update_team_budgets(
                    conn, team_id, body.response_budget_min, body.resolution_budget_min
                )
        finally:
            conn.close()

    @app.put("/teams/{team_id}/calendar/{day}")
    def put_calendar(team_id: int, day: str, body: CalendarIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                result = service.set_team_calendar(
                    conn, team_id, day, body.intervals, body.actor, now
                )
            return result
        finally:
            conn.close()

    @app.get("/teams/{team_id}/calendar/{day}")
    def get_calendar(team_id: int, day: str):
        conn = get_conn()
        try:
            service.get_team(conn, team_id)
            return {"team_id": team_id, "day": day, "intervals": service.day_calendar(conn, team_id, day)}
        finally:
            conn.close()

    @app.get("/teams/{team_id}/corrections")
    def list_corrections(team_id: int):
        conn = get_conn()
        try:
            service.get_team(conn, team_id)
            rows = conn.execute(
                "SELECT * FROM calendar_corrections WHERE team_id = ? ORDER BY id", (team_id,)
            ).fetchall()
            import json as _json

            return [
                {
                    "id": r["id"],
                    "day": r["day"],
                    "before": _json.loads(r["before_json"]),
                    "after": _json.loads(r["after_json"]),
                    "actor": r["actor"],
                    "created_at": timeutil.iso(r["created_at"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ---------------- 时钟 ----------------

    @app.get("/clock")
    def get_clock():
        conn = get_conn()
        try:
            return service.clock_info(conn)
        finally:
            conn.close()

    @app.post("/clock")
    def set_clock(body: ClockIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                at = service.set_clock(conn, _at(body.at), body.actor)
                service.reevaluate_open_tickets(conn, at)
                # 回拨高水位只增不减：时钟推进可能使未开始预约错过，时钟回放不复活。
                conn.execute(
                    "INSERT INTO callback_clock (id, high_water_minute) VALUES (1, ?)"
                    " ON CONFLICT(id) DO UPDATE SET high_water_minute ="
                    " MAX(high_water_minute, excluded.high_water_minute)",
                    (at,),
                )
                cbmod.sweep_missed(conn, cbmod.business_now(conn))
            return service.clock_info(conn)
        finally:
            conn.close()

    # ---------------- 工单 ----------------

    @app.post("/tickets", status_code=201)
    def create_ticket(body: TicketIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                at = now if body.at is None else _at(body.at)
                if at > now:
                    raise service.Conflict("登记时刻不能晚于当前时刻")
                ticket_id = service.create_ticket(
                    conn,
                    body.title,
                    body.team_id,
                    at,
                    body.actor,
                    body.response_budget_min,
                    body.resolution_budget_min,
                    body.accept,
                )
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/accept")
    def accept(ticket_id: int, body: AcceptIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.accept_ticket(conn, ticket_id, body.actor, _at(body.at), now)
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.get("/tickets")
    def list_tickets(status: str | None = None, team_id: int | None = None):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.reevaluate_open_tickets(conn, now)
                cbmod.sweep_missed(conn, cbmod.business_now(conn))
            return service.list_tickets(conn, status, team_id)
        finally:
            conn.close()

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: int):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.evaluate_ticket(conn, ticket_id, now)
                cbmod.sweep_missed(conn, cbmod.business_now(conn))
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.get("/tickets/{ticket_id}/events")
    def get_events(ticket_id: int):
        conn = get_conn()
        try:
            service.ticket_detail(conn, ticket_id)
            return service.list_events(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/notes")
    def add_note(ticket_id: int, body: NoteIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.add_note(conn, ticket_id, body.actor, body.content, _at(body.at), now)
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/replies")
    def add_reply(ticket_id: int, body: ReplyIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                at = _at(body.at)
                if body.customer:
                    service.add_customer_reply(conn, ticket_id, body.actor, body.content, at, now)
                else:
                    service.add_agent_reply(
                        conn, ticket_id, body.actor, body.useful, body.content, at, now
                    )
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/customer-replies")
    def add_customer_reply(ticket_id: int, body: CustomerReplyIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.add_customer_reply(conn, ticket_id, body.actor, body.content, _at(body.at), now)
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/waits")
    def request_wait(ticket_id: int, body: WaitIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.request_customer_info(
                    conn, ticket_id, body.actor, body.reason, _at(body.at), now
                )
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/resolve")
    def resolve(ticket_id: int, body: ResolveIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.resolve_ticket(conn, ticket_id, body.actor, _at(body.at), now)
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    @app.post("/tickets/{ticket_id}/transfer")
    def transfer(ticket_id: int, body: TransferIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                service.transfer_ticket(
                    conn, ticket_id, body.to_team_id, body.actor, _at(body.at), now
                )
            return service.ticket_detail(conn, ticket_id)
        finally:
            conn.close()

    # ---------------- 升级 ----------------

    @app.get("/escalations")
    def list_escalations(ticket_id: int | None = None, status: str | None = None):
        conn = get_conn()
        try:
            return service.list_escalations(conn, ticket_id, status)
        finally:
            conn.close()

    @app.get("/escalations/{escalation_id}")
    def get_escalation(escalation_id: int):
        conn = get_conn()
        try:
            return service.get_escalation(conn, escalation_id)
        finally:
            conn.close()

    @app.post("/escalations/{escalation_id}/acknowledge")
    def acknowledge(escalation_id: int, body: AckIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                result = service.acknowledge_escalation(conn, escalation_id, body.actor, body.note, now)
            return result
        finally:
            conn.close()

    # ---------------- 回拨：坐席档案 ----------------

    @app.post("/agents", status_code=201)
    def create_agent(body: AgentIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                agent = cbmod.create_agent(
                    conn, body.name, body.team_id, body.skills, body.availability, now
                )
            return agent
        finally:
            conn.close()

    @app.get("/agents")
    def list_agents(team_id: int | None = None):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                return cbmod.list_agents(conn, team_id)
        finally:
            conn.close()

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: int):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                return cbmod.agent_detail(conn, agent_id)
        finally:
            conn.close()

    @app.patch("/agents/{agent_id}")
    def patch_agent(agent_id: int, body: AgentPatchIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                return cbmod.update_agent(
                    conn, agent_id, body.name, body.team_id, body.skills, body.availability, now
                )
        finally:
            conn.close()

    # ---------------- 回拨：预约与执行 ----------------

    @app.post("/tickets/{ticket_id}/callbacks", status_code=201)
    def book_callback(ticket_id: int, body: CallbackBookIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                result = cbmod.book_callback(
                    conn, ticket_id, body.skill,
                    cbmod.parse_minute(body.start), cbmod.parse_minute(body.end),
                    body.actor, now, body.agent_id,
                )
            return result
        finally:
            conn.close()

    @app.get("/tickets/{ticket_id}/callbacks")
    def list_ticket_callbacks(ticket_id: int):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                service._ticket_row(conn, ticket_id)
                return cbmod.list_callbacks(conn, ticket_id=ticket_id)
        finally:
            conn.close()

    @app.get("/callbacks")
    def list_callbacks(ticket_id: int | None = None, status: str | None = None,
                       team_id: int | None = None, agent_id: int | None = None):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                return cbmod.list_callbacks(conn, ticket_id, status, team_id, agent_id)
        finally:
            conn.close()

    @app.get("/callbacks/{callback_id}")
    def get_callback(callback_id: int):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                cbmod.sweep_missed(conn, now)
                return cbmod.get_callback(conn, callback_id)
        finally:
            conn.close()

    @app.post("/callbacks/{callback_id}/reschedule")
    def reschedule_callback(callback_id: int, body: CallbackRescheduleIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                result = cbmod.reschedule_callback(
                    conn, callback_id,
                    cbmod.parse_minute(body.start), cbmod.parse_minute(body.end),
                    body.actor, now,
                )
            return result
        finally:
            conn.close()

    @app.post("/callbacks/{callback_id}/cancel")
    def cancel_callback(callback_id: int, body: CallbackCancelIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                result = cbmod.cancel_callback(conn, callback_id, body.actor, now, body.reason)
            return result
        finally:
            conn.close()

    @app.post("/callbacks/{callback_id}/start")
    def start_callback(callback_id: int, body: CallbackStartIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                result = cbmod.start_callback(
                    conn, callback_id, body.agent_id, body.actor, now, _at(body.at)
                )
            return result
        finally:
            conn.close()

    @app.post("/callbacks/{callback_id}/end")
    def end_callback(callback_id: int, body: CallbackEndIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = cbmod.business_now(conn)
                result = cbmod.end_callback(conn, callback_id, body.actor, now, _at(body.at))
            return result
        finally:
            conn.close()

    # ---------------- 共享故障通报 ----------------

    @app.post("/incidents", status_code=201)
    def create_incident(body: IncidentIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                return incmod.create_incident(conn, body.title, body.description, body.actor, now)
        finally:
            conn.close()

    @app.get("/incidents")
    def list_incidents():
        conn = get_conn()
        try:
            return incmod.list_incidents(conn)
        finally:
            conn.close()

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: int):
        conn = get_conn()
        try:
            return incmod.get_incident(conn, incident_id)
        finally:
            conn.close()

    @app.post("/incidents/{incident_id}/links", status_code=201)
    def link_ticket(incident_id: int, body: LinkIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                return incmod.link_ticket(
                    conn, incident_id, body.ticket_id, body.customer_id, body.actor, now
                )
        finally:
            conn.close()

    @app.post("/incidents/{incident_id}/unlinks")
    def unlink_ticket(incident_id: int, body: UnlinkIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                return incmod.unlink_ticket(conn, incident_id, body.ticket_id, body.actor, now)
        finally:
            conn.close()

    @app.get("/incidents/{incident_id}/links")
    def list_incident_links(incident_id: int, active: bool = False):
        conn = get_conn()
        try:
            return incmod.list_links(conn, incident_id, active_only=active)
        finally:
            conn.close()

    @app.put("/customers/{customer_id}/notification")
    def set_notification(customer_id: str, body: NotificationIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                return incmod.set_notification(conn, customer_id, body.enabled, body.actor, now)
        finally:
            conn.close()

    @app.get("/customers/{customer_id}/notification")
    def get_notification(customer_id: str):
        conn = get_conn()
        try:
            return incmod.get_notification(conn, customer_id)
        finally:
            conn.close()

    @app.post("/incidents/{incident_id}/updates", status_code=201)
    def publish_update(incident_id: int, body: UpdateIn):
        conn = get_conn()
        try:
            with db.transaction(conn):
                now = service.now_minute(conn)
                return incmod.publish_update(conn, incident_id, body.body, body.actor, now)
        finally:
            conn.close()

    @app.get("/incidents/{incident_id}/updates")
    def list_incident_updates(incident_id: int):
        conn = get_conn()
        try:
            return incmod.list_updates(conn, incident_id)
        finally:
            conn.close()

    @app.get("/updates/{update_id}")
    def get_update(update_id: int):
        conn = get_conn()
        try:
            return incmod.get_update(conn, update_id)
        finally:
            conn.close()

    @app.get("/updates/{update_id}/deliveries")
    def list_update_deliveries(update_id: int, status: str | None = None):
        conn = get_conn()
        try:
            return incmod.list_deliveries(conn, update_id=update_id, status=status)
        finally:
            conn.close()

    @app.get("/incidents/{incident_id}/deliveries")
    def list_incident_deliveries(incident_id: int, status: str | None = None,
                                 customer_id: str | None = None):
        conn = get_conn()
        try:
            return incmod.list_deliveries(
                conn, incident_id=incident_id, status=status, customer_id=customer_id
            )
        finally:
            conn.close()

    @app.get("/deliveries/{delivery_id}")
    def get_delivery(delivery_id: int):
        conn = get_conn()
        try:
            return incmod.get_delivery(conn, delivery_id)
        finally:
            conn.close()

    @app.get("/deliveries/{delivery_id}/attempts")
    def get_delivery_attempts(delivery_id: int):
        conn = get_conn()
        try:
            return incmod.list_attempts(conn, delivery_id)
        finally:
            conn.close()

    @app.post("/delivery/run")
    def run_delivery(body: DeliveryRunIn | None = None):
        # 推进入口：并行调用或服务重启后反复调用都是安全的（租位 + 发送端幂等）。
        kwargs = {}
        if body is not None and body.lease_seconds is not None:
            kwargs["lease_seconds"] = body.lease_seconds
        conn = get_conn()
        try:
            return delivery_worker.run_cycle(conn, app.state.sender, **kwargs)
        finally:
            conn.close()

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


# 供 uvicorn 直接使用
app = create_app()
