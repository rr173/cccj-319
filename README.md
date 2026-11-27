# 承诺时限后端

多地客服团队共用一套工单系统的承诺时限（SLA）后端，Python + FastAPI + SQLite。

## 运行

```bash
python3 -m pip install fastapi uvicorn
TICKET_DB=./tickets.db python3 -m uvicorn app.main:app --reload
# 交互式文档: http://127.0.0.1:8000/docs
```

测试（无需外部依赖，测试用临时库）：

```bash
python3 -m pytest tests -q
```

## 时间口径

- 一切时间用**整数分钟**（UTC epoch minutes）表达；前端可用 ISO-8601 传入
  （`2026-01-01T09:00:00Z`），服务端转换成分钟。
- 工作区间是**半开区间** `[start_minute, end_minute)`，按 UTC 日设定，
  `0 <= start < end <= 1440`，同一天内互不重叠。
- 值班员可通过 `POST /clock` 登记“可控当前时刻”，所有未完成工单按它计算已耗；
  `GET /clock` 返回当前口径。

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 登记时冻结响应、解决两个工作分钟预算 | 预算复制进 `tickets`，之后改团队默认值不影响老工单 |
| 响应耗时 = 受理 → 首次**有用**答复 | 普通客服留言 / 内部备注不算有用答复 |
| 解决耗时 = 受理 → 解决，仅等待客户补资料暂停 | 等待只能由客服发起（`POST /waits`），只由**关联的客户回复**结束；普通留言不恢复 |
| 明确转组后按新团队日历继续 | 按转组时刻切段，各段用“当时负责团队”的日历累加，**不归零** |
| 完成恰好用完预算不算超时 | 判定为 `elapsed > budget` |
| 未完成查询给出已耗与余额 | `elapsed_min` / `remaining_min` / `over_by_min` |
| 响应与解决分别管理 | 两个维度独立计时、独立判定、独立升级 |

## 日历更正与升级

- `PUT /teams/{id}/calendar/{day}`：值班员更正**已过时段**的团队日历，立即重算
  所有跨越该时段工单的当前判定与余额。更正留档在 `calendar_corrections`。
- 查询同时给出三层信息：
  - `original`：**原事实**，第一次判定（首次超时或结案）时定格，之后永不改写；
  - `current`：**当前判定**，随日历更正 / 时间推进重算；
  - `escalation`：**升级处理结果**（`open` / `acknowledged` / `cancelled` / `closed`）。
- 更正使原超时变为未超时：
  - 未签收的升级 → `cancelled`（撤销），**不能被签收**；
  - 已签收的升级 → 不可删除，转为 `closed` 并保留 `closure_note = "判断已更正"`。
- 更正后**再次**超时：旧升级不会复活，另开一条新的可签收记录（`seq` 递增）。
- 连续超时（一直处于超时状态）不会反复开单。
- 完成不能撤销已形成的超时；只有更正后完成耗时未超预算才改判。

## 并发

所有写请求在 `BEGIN IMMEDIATE` 事务内执行，SQLite 串行化写者；签收时在同一事务
内读取升级状态，已撤销的升级返回 `409`。日历更正与签收竞争时最终只会有一个合法
结果：签收成功则升级转 `closed`，否则为 `cancelled`。

## 主要接口

```
POST   /teams                                  建团队（含默认预算）
PATCH  /teams/{id}                             改团队默认预算（不影响已登记工单）
PUT    /teams/{id}/calendar/{day}              设定/更正某日工作区间
GET    /teams/{id}/calendar/{day}
GET    /teams/{id}/corrections                 日历更正历史
GET    /clock   POST /clock                    读/登记可控当前时刻

POST   /tickets                                登记（accept 默认 true；false 则先登记）
POST   /tickets/{id}/accept                    受理（开始计时）
GET    /tickets            ?status= &team_id=
GET    /tickets/{id}                           含原事实/当前判定/升级/暂停明细
GET    /tickets/{id}/events                    行为流水
POST   /tickets/{id}/notes                     普通留言（不影响答复判定）
POST   /tickets/{id}/replies                   客服答复（useful=true 才算响应完成）
POST   /tickets/{id}/customer-replies          客户回复（结束关联等待）
POST   /tickets/{id}/waits                     发起等待客户补资料
POST   /tickets/{id}/resolve                   解决
POST   /tickets/{id}/transfer                  转组

GET    /escalations        ?ticket_id= &status=
POST   /escalations/{id}/acknowledge           签收
```

## 数据模型

`tickets` 上的 `response_* / resolution_*` 列分别保存原事实与当前判定；升级存放在
`escalations`（每个工单每个维度每波超时一条，唯一索引 `(ticket, dimension, seq)`，
形成后不可删除）；日历更正、行为流水、等待、转组各有独立表，便于审计。
