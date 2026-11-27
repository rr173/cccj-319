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

## 回拨预约

客服与客户确认回拨窗口后，内部调班/转组不能把内部变化变成客户失约。回拨使用与
团队时限日历**相互独立**的坐席可用时段，仍以整数分钟、半开区间 `[start, end)` 表达。

### 角色与接口

```
POST   /agents                                 主管登记坐席（团队、技能、可用时段）
GET    /agents                       ?team_id=
GET    /agents/{id}
PATCH  /agents/{id}                            调整团队/技能/可用时段（全局可行性校验）

POST   /tickets/{id}/callbacks                 客服按技能+窗口预约（可带 agent_id 意向）
GET    /tickets/{id}/callbacks                 工单的全部预约
GET    /callbacks        ?ticket_id=&status=&team_id=&agent_id=
GET    /callbacks/{id}
POST   /callbacks/{id}/reschedule              改期（仅未开始）
POST   /callbacks/{id}/cancel                  取消（仅未开始）
POST   /callbacks/{id}/start                   坐席在窗口内开始，开始后固定执行坐席
POST   /callbacks/{id}/end                     执行坐席登记结束，可补记
```

时间字段同时接受 ISO-8601 字符串与整数 epoch 分钟；响应同时给出两者。

### 核心不变量

| 规则 | 说明 |
| --- | --- |
| 一单一笔活跃 | 每张已受理、未结案工单同时最多一笔 `pending`/`in_progress`（部分唯一索引兜底） |
| 整窗覆盖 | 执行坐席必须属于工单负责组、具备所需技能、可用时段覆盖**整个**窗口，不能分段换人 |
| 坐席不重叠 | 同一坐席的有效预约时间互不重叠 |
| 全局可行性 | 新约/改期/调班/调技能都要验证调整后**所有尚有效预约可同时兑现**；pending 预约可在合格坐席间重新分配，不可行则整体拒绝，原承诺不变（事务回滚，无部分更新） |
| 开始即锁定 | 只能在约定窗口内开始；开始后固定执行坐席，不能改期/取消，开始操作不排挤其他已确认预约 |
| 执行坐席保护 | 回拨仍占容量期间，调班/改技能不能破坏执行坐席对原窗口的覆盖 |
| 结束释放 | 提前结束释放余下时段；窗口到点自然释放容量，仍允许补记结束 |
| 错过不可逆 | 未开始即到窗口终点记为 `missed` 并释放容量；时钟回放不能复活 |
| 转组承接 | 未开始预约保留客户窗口与技能由目标组承接，安排不下则不转组；已开始未结束禁止转组/结案 |
| 结案取消 | 结案取消仍未开始且未过期的预约；已开始未结束须先结束才能结案 |
| 不影响 SLA | 回拨登记不是有用答复/解决，原工作分钟预算、暂停与升级行为完全保留 |

### 回拨业务时刻（高水位）

回拨复用现有可控时钟，但业务时刻取“可控时钟”与“回拨操作/时钟登记已到达的最晚
分钟”的较大者（存于 `callback_clock`，单调不回落）。因此时钟可以像原来一样回放以
重算 SLA，但已错过的回拨不会复活，也不能把新约/改期订到高水位之前。原 SLA 的
回放口径不变。

### 数据模型（回拨部分）

- `agents` / `agent_skills` / `agent_availability`：坐席所属团队、技能集合、绝对
  分钟半开可用时段（同坐席互不重叠）。
- `callbacks`：预约主表，状态机 `pending → in_progress → completed`，旁路
  `cancelled` / `missed`；部分唯一索引保证一单一笔活跃。
- `callback_clock`：单行高水位。
- 回拨动作写入既有 `ticket_events`（`callback_booked/rescheduled/cancelled/started/ended/missed/transferred`）。
