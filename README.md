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

## 共享故障通报

同一故障常让客服收到多张工单。客服可登记故障、把工单（连同客户标识）关联到
故障并发布进展；系统按发布时刻锁定的名单逐客户投递，并可查询逐客户投递结果。
通报是**独立域**：不算工单回复或结案，不写 `ticket_events`，不改变 SLA 预算、
升级与回拨的任何行为。

### 接口

```
POST   /incidents                               登记故障
GET    /incidents                        ?id=
GET    /incidents/{id}
POST   /incidents/{id}/links                   关联工单（带 customer_id）
POST   /incidents/{id}/unlinks                 撤销关联（ticket_id）
GET    /incidents/{id}/links           ?active=
PUT    /customers/{customer_id}/notification   开启/关闭通知（{enabled: bool}，默认开启）
GET    /customers/{customer_id}/notification
POST   /incidents/{id}/updates                 发布进展（锁定正文与通知名单）
GET    /incidents/{id}/updates
GET    /updates/{id}                            含四态计数 status_counts
GET    /updates/{id}/deliveries        ?status=pending|skipped|in_flight|delivered
GET    /incidents/{id}/deliveries      ?status=&customer_id=
GET    /deliveries/{id}                         单条投递（含发布时关联快照）
GET    /deliveries/{id}/attempts                投递尝试/跳过流水
POST   /delivery/run                            推进投递（可并行、可反复调用）
```

CLI 一次性推进（服务重启后重跑即可恢复未确认投递）：

```bash
TICKET_DB=./tickets.db NOTIFY_SENDER_DB=./sender.db \
  python3 -m app.delivery_worker --db ./tickets.db
```

### 发布语义（锁定名单，不补发）

- 发布在一个 `BEGIN IMMEDIATE` 事务内定格正文与名单：当时有**有效关联**且
  **开启通知**的客户才进入名单；同一客户多张工单只生成一条投递，其全部有效
  关联写入该投递的发布快照（`incident_delivery_links`）。
- 发布后才入组的关联、发布后才开启的通知，不会补发旧进展。
- 同一工单在同一故障下只有一条有效关联（部分唯一索引
  `idx_incident_links_one_active` 兜底；重复关联返回 `409`）。

### 投递状态机与永久跳过

```
pending ──┬─> skipped        （永久跳过，终态）
          └─> in_flight ──> delivered   （确认送达，终态）
```

四种查询口径与状态一一对应：`pending` 待处理、`skipped` 已跳过、`in_flight`
已决定交付但结果待确认、`delivered` 已确认送达。

- 跳过决定**只在 pending 阶段做一次**，两个来源：
  - `unsubscribed`：发布之后到决定之前客户曾关闭通知。关闭动作把该客户所有
    pending 投递置 `tainted=1`，即使随后重新开启也不消除；
  - `no_active_link`：发布快照中的关联已**全部**撤销。只要快照里还有一条
    持续有效，撤掉其他关联不影响投递；发布后重新关联/新增工单不在快照内，
    不能恢复投递。
- 进入 `in_flight` 即已做出交付决定：之后的撤订、撤关联只影响后续进展，
  本投递继续重试到确认送达，不会停止。
- 同故障同客户严格按进展序号交付：更早一条未终结（pending/in_flight）时，
  后一条保持 pending；某客户被门控不影响其他客户。不同故障之间互不门控。

### 可替换发送端与本地替身

- 发送端接口见 `app/sender.py` 的 `Sender`：接收稳定投递标识 `delivery_key`、
  客户标识与正文；接入真实渠道只需实现同接口并在 `build_sender()` 装配。
- 本地替身 `StubSender` 把回执持久化在独立 SQLite（`NOTIFY_SENDER_DB`，
  默认内存库；给文件路径即可跨重启保留），按 `delivery_key` 幂等：
  同一标识重试只触达一次。支持故障注入：
  `queue_behavior(key, "pre_timeout"|"post_timeout"|"error"|"ok")` 一次性行为、
  `stick_behavior` 持续行为、`clear_behaviors` 清除。
  - `pre_timeout`：接收前超时，无回执，重试才真正触达；
  - `post_timeout`：先落回执再超时，重试只得到幂等重放，绝不二次触达。
- `POST /delivery/run` 是推进入口：每个决定在独立短事务内完成，发送调用在
  事务外。`in_flight` 带真实时间租位（`lease_owner/lease_until`），并行 worker
  靠条件更新抢占；进程崩溃后租约过期，下一轮（或重启后）自动接管。
  因此并行推进、服务重启都不会漏发或重复触达。

### 数据模型（通报部分）

- `incidents` / `incident_links`：故障与带客户标识的工单关联，撤销只置
  `revoked_at`，历史行保留。
- `notification_settings`：客户通知开关，无记录即默认开启。
- `incident_updates`：进展正文与故障内递增序号，发布即不可变。
- `incident_deliveries`：逐客户投递（四态、`tainted`、跳过原因、租位、
  尝试次数、稳定 `delivery_key`）。
- `incident_delivery_links`：发布时刻关联快照，跳过判定的唯一依据。
- `incident_delivery_attempts`：跳过与每次发送尝试的审计流水。

## 企业客户赔付核销

企业客户的工单超过解决时限时，客服合同赔付可抵扣发票的额度。财务登记企业
账户与固定条款、把工单归入账户，随后可预占额度、确认抵扣、整笔冲销；值班员
追溯更正工作日历后，财务仍能按原凭据对账。不接入实际开票或支付系统。金额
一律**整数分**。核销是**独立域**：只读取工单的结案时刻与 resolution 当前
判定，原时限、升级、回拨与通报行为不变。

### 接口

```
POST   /credit-accounts                              登记企业账户（每张超时工单赔付额、每月赔付上限）
GET    /credit-accounts        GET /credit-accounts/{id}
POST   /credit-accounts/{id}/ticket-assignments      把已有工单一次性归入（结案后也可补登记）
GET    /credit-accounts/{id}/tickets                 已归入工单
GET    /credit-accounts/{id}/reconciliation          账户对账（应得/余额/未弥补额/申请/凭据）

POST   /credit-accounts/{id}/credit-requests         申请预占（发票编号 + 申请编号 + 金额）
GET    /credit-accounts/{id}/credit-requests ?status=
GET    /credit-requests/{id}                         含有效预占的当前来源
POST   /credit-requests/{id}/cancel                  取消有效预占
POST   /credit-requests/{id}/confirm                 确认并出具抵扣凭据

GET    /credit-accounts/{id}/vouchers
GET    /credit-vouchers/{id}                         凭据（含来源行与冲销结果）
POST   /credit-vouchers/{id}/reverse                 整笔冲销已确认抵扣
```

### 应得额度口径

- 仅**已结案**且按现有口径**当前判为解决超时**的工单有资格；结案、归属登记、
  日历更正都会改变应得（包括同月其他工单）。
- 按结案时刻的 **UTC 月份**归账；同月按（结案时刻, 工单编号）依次发放
  `min(每张赔付额, 当月剩余上限)`，剩余不足只给余量。
- 填补与预占使用额度时**跨月可合用**，都优先用结案更早的剩余额度，同刻按
  工单编号。

### 预占 / 凭据 / 冲销

- 申请预占必须**全额有着落**才接受；有效预占按原受理顺序分配，来源可随状态
  重排；当前不能全额兑现的一笔**立即永久失效**且不占额度（写事务内持久化，
  之后额度恢复、他人取消或抵扣冲销都不得复活），后面的申请仍可用余额。
- 确认出具**不可改写**的抵扣凭据，金额来源 = 该笔有效预占当时分得的工单；
  改判不撤回凭据、也不改记别的工单。
- 某工单当前应得不足以覆盖其未冲销抵扣时（含改判归零），差额先由账户其他
  工单未抵扣额度填补，仍不足记**未弥补额**；填补优先于一切有效预占。
- 冲销只能**整笔**退回已确认抵扣：释放其占用，保留原凭据与冲销结果。
- 幂等：申请编号在账户内唯一并永久绑定原请求（同号重试按原参数重放，参数
  不符返回 409）；取消/确认/冲销重试只重放结果，不重复扣减或释放。
  同企业同发票同时最多一笔有效预占或未冲销抵扣（部分唯一索引兜底）。

### 一致性与持久性

所有核销写操作在 `BEGIN IMMEDIATE` 事务内执行；结案、日历更正、归属登记在
同事务末尾统一重算账户分配——一次更正影响多张工单时按最终一致状态结算，
中间结果不会使预占失效；即使改判后无人查询，失效也已落库，随后额度回升
不能掩盖。失败请求整体回滚不留部分变更；全部状态存于 SQLite，服务重启
不丢失。

### 数据模型（核销部分）

- `credit_accounts`：企业账户与固定条款（每张赔付额、每月上限，整数分）。
- `credit_ticket_assignments`：工单归属，一单一户（唯一索引），结案前后均可登记。
- `credit_requests`：预占申请（`valid/cancelled/invalid/confirmed`），
  `(account_id, application_no)` 唯一；`invalid` 为终态。
- `deduction_vouchers` / `deduction_voucher_lines`：抵扣凭据与来源行，
  出具后不可改写；`active/reversed`。
- `deduction_reversals`：冲销记录，一笔凭据最多一次。
