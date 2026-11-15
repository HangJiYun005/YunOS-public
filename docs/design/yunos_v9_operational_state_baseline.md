# YunOS v9.0 — Personal Operational State Design Baseline

版本名称：**YunOS v9 — Personal Operational State**
节点性质：设计、边界与实施路线锁定
权威起点：`9a1794d8b997e559d33750ee046c6649683aa778`
权威 v8 tag：`v8.3-unified-ingestion`
工作分支：`feature/v9-personal-operational-state`

## 1. 结论

v9 建立“用户当前在做什么”的外部操作层。它不替代 v7 长期 Memory，不复制 v8
Runtime Ledger，也不把统一导入事件改名为状态事件。

仓库审计未发现必须把状态写入现有四库的硬约束，因此采用独立第五库：

`data/operational/ayunos_operational_state.db`

首代且唯一的 v9 schema version 为 `9001`。普通 Web、Tailnet 和移动端只读；生产状态
writer 默认关闭。首版写入只通过本地 `state-admin` CLI 完成，使用 authority artifact、
preview/commit 分离、idempotency key、单 writer lease、receipt 和 append-only audit。

v9.0 设计基线 **GO**。这不是生产迁移授权；生产第五库只能在 v9-F 的备份、克隆迁移、
回滚、恢复、回归和服务静默门禁全部通过后创建。

## 2. 仓库审计与可复用边界

### 2.1 v6-v7

复用：

- `Memory Graph`、Evidence Identity、Source Anchor 的稳定身份思想；
- `Memory Package`、Evidence Selection、Context Assembly 的 direct/approved 优先、
  budget、dedup、omitted reason、canonical digest 和 source-backed 校验；
- Temporal Memory Candidate 的“候选不是事实”边界；
- Goal、Decision、Constraint、Conflict 的已批准记录。

不复用为写入权威：

- 完整对话正文；
- 未批准 temporal candidate；
- 全历史自动回填；
- Memory 的长期画像更新机制。

Operational State 只保存当前项目状态和必要 source anchor，不建立第二套 Memory。

### 2.2 v8.0 Trusted Runtime

复用 Preview、Reviewed Binding、Execution Authority、Receipt、Verification、fail-closed、
exactly-once/at-most-once 保护和恢复边界。

严格保留：

`runtime succeeded != reported outcome != verified outcome != accepted outcome`

Operational State 只保存 Runtime run/receipt/verification/artifact 的引用和 digest，不复制
Runtime Ledger 的消息、请求或执行事件。

### 2.3 v8.1 Desktop Product

复用 Omnibox、Contextual Sidebar、文档式结果、现有 Design Tokens、键盘/focus-visible
和响应式 shell。v9-E 只增加 Now、Projects、Waiting、Decisions、Delegations 五个入口，
不建设通用项目管理后台。

### 2.4 v8.2 Private Mobile Access

复用 loopback-only host、Tailnet-only HTTPS、GET-only API、`Cache-Control: no-store`、
Visual Viewport 和应用壳/私人缓存分离。移动端永不获得状态写接口。离线状态必须显示
`cached_at` 和 stale，不得伪装实时状态。

### 2.5 v8.3 Unified Ingestion

复用 canonical JSON、deterministic IDs、ApprovalArtifact、single-writer lease、迁移克隆、
failure injection、backup/restore 和 schema drift 检测的实现方式。

不复用语义：

- ingestion event 不是 StateEvent；
- ImportBatch 不是 Project/WorkItem；
- ingestion approval 不能批准 Operational State candidate；
- v8302、Historical Reconstruction 和 Historical Backfill 保持
  `preserved_out_of_scope_engineering`。

## 3. 产品范围

v9 必须可靠表达：

- Project、Goal、WorkItem、Decision、Commitment、Blocker、Resource；
- Delegation、Outcome 以及 reported/verified/accepted 的不同阶段；
- Declared、Observed、Inferred Candidate 三类来源；
- 当前焦点、下一批准步骤、下一候选步骤、waiting、pending decision；
- fresh、aging、stale、expired、superseded、conflicted、unverifiable；
- source anchor、authority、verification、writer lease；
- Now Package 和 Project Resume Package。

## 4. 非目标

v9 不做：

- 主动 Agent、无人监督持续执行或 Codex 工作自动创建；
- 人生规划、价值观推断或长期人格画像更新；
- v9-G、v9.1、v10 或第二套并行框架；
- 企业成员权限、多租户、工时、Sprint、Kanban 或客户系统；
- 全历史聊天自动回填；
- 移动写入、Tailnet 写入或 SQLite 多设备同步；
- Historical Reconstruction、v8302 或另一次导入。

## 5. 存储架构决策

| 方案 | 结论 | 原因 |
|---|---|---|
| Main DB 增表 | 拒绝 | 污染长期记忆与导入 schema，状态写频率和恢复边界不同 |
| Runtime DB 增表 | 拒绝 | Runtime 是执行证据账本，不能承载用户当前状态 |
| 独立 Operational State DB | 采用 | 可单独迁移、备份、恢复、lease、projection rebuild 和只读发布 |

正式路径：

`data/operational/ayunos_operational_state.db`

测试、克隆、迁移演练和恢复实例不得使用该路径。生产创建只发生一次，使用 schema
`9001`，默认 writer off。

## 6. 领域模型

### 6.1 StateEntity

稳定身份载体。`entity_id` 由 entity type、canonical external key 和 namespace 确定性
生成。允许类型：

`project`, `goal`, `work_item`, `decision`, `commitment`, `blocker`, `resource`,
`delegation`, `outcome`

### 6.2 Project

合法状态：`proposed -> active <-> paused -> completed -> archived`。completed 必须有明确
接受或等价确定性规则；观察到 Git commit 不能单独宣布 Project completed。

### 6.3 Goal

`candidate -> approved -> active <-> paused -> achieved`，并可进入 `abandoned` 或
`superseded`。Agent 不能批准 Goal candidate。

### 6.4 WorkItem

`proposed -> ready -> in_progress -> completed`；`in_progress` 可进入 `blocked` 或
`waiting` 后恢复；可进入 `cancelled`/`superseded`。Delegation completed 不能直接令
WorkItem completed，必须有 verification 和 acceptance。

### 6.5 Decision

`candidate -> approved|rejected`，approved 可进入 `superseded|revoked`。

### 6.6 Commitment

`active -> fulfilled|cancelled|expired|breached`。

### 6.7 Blocker

`open -> mitigated -> resolved`，也可由确定性证据进入 `obsolete`。Agent 无权自行解除
Blocker。

### 6.8 Delegation

`proposed -> authorized -> queued -> running -> waiting -> completed|failed|cancelled
-> accepted|rejected`

`completed` 是 Agent 报告；verification 和 Outcome acceptance 是独立事件。

### 6.9 Outcome

`reported -> verified -> accepted|rejected`，也可 `superseded`。失败结果同样写入审计。

## 7. 事件模型

唯一事实源为：

`Append-only StateEvent -> Deterministic Projection -> Current State View`

StateEvent 包含：

- `event_id`, `entity_id`, `entity_type`, `event_type`;
- `source_kind`, `source_anchor`, `authority_kind`;
- `actor_kind`, `actor_id`;
- `observed_at`, `effective_at`, `recorded_at`, `expires_at`;
- `supersedes_event_id`, `caused_by_event_id`;
- `idempotency_key`, `conflict_group_id`;
- `canonical_payload`, `payload_digest`, `privacy_scope`;
- `projection_version`, `schema_version`.

规则：

1. 全部时间规范为 UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`。
2. `recorded_at` 和数据库 sequence 构成审计顺序；projection 以 authority resolver、
   `effective_at`、`recorded_at`、`event_id` 确定性裁决。
3. duplicate event ID 或 idempotency key 只返回原 receipt；不同 payload 必须拒绝。
4. 乱序事件保留并重算 projection，不覆盖或删除旧事件。
5. 撤销与替代通过新事件和 `supersedes_event_id` 表达。
6. 并列且不能安全裁决的声明进入 conflict group，不静默选边。
7. UPDATE/DELETE StateEvent 由 SQLite trigger 阻止。
8. projection 更新与 event append 在一个 `BEGIN IMMEDIATE` 事务中原子完成。
9. 可从 genesis 或受验证 snapshot 重建；任何 digest 不一致 fail-closed。

## 8. 状态来源

### Declared State

用户明确声明、批准、撤销或替代。用户意图、优先级、接受和重大决定由最新有效 Declared
State 决定。

### Observed State

只来自确定性 Git、数据库、文件、tag、test、Runtime Receipt/Verification 或服务状态。
每个观察必须有 source anchor、observed_at 和可复验摘要。

### Inferred Candidate

模型或启发式推测。只进入 candidate 表，必须有 confidence、evidence 和 expiry；不能
进入权威 projection，不能覆盖声明，必须由用户批准或显式确定性转换规则处理。

## 9. Authority Matrix

| 字段/状态 | 权威 resolver |
|---|---|
| 用户意图、当前优先级、项目接受完成 | 最新有效 Declared State |
| Git branch/HEAD/tag/worktree | 最新确定性 Git Observation，每次读取重新确认 |
| 数据库 hash/schema/count/integrity/FK | 最新确定性 DB Observation |
| Runtime execution success | Receipt + Verification |
| Outcome accepted | 用户接受事件或本任务内明确授权的验收规则 |
| service/Tailnet/Funnel | 极短 TTL service observation |
| candidate | 永不自动成为权威 |

禁止全局分数。每个字段 resolver 明确 source kind、authority kind、freshness 和冲突规则。
Agent 不能批准自己的结果、扩大 authority、裁决 pending conflict 或宣布下一个版本。

## 10. Freshness

| 状态类型 | 策略 |
|---|---|
| Git HEAD/worktree | 每次包生成时复验 |
| 当前焦点 | 15 分钟 fresh，60 分钟 aging，之后 stale |
| 服务状态 | 30 秒 fresh，2 分钟 aging，之后 stale |
| Delegation running | 2 分钟 heartbeat；5 分钟 aging；10 分钟 stale |
| 项目阶段 | 事件驱动；7 天未复验为 aging，30 天 stale |
| 长期目标 | 无短 TTL；90 天复核 |
| 已批准决定 | 直到 revoked/superseded |
| verified Outcome | 持久，acceptance 独立 |

到期状态不删除。freshness recalculation 产生投影元数据或新事件，保留原始事实。

## 11. 数据库逻辑结构

- `operational_schema`：schema marker/digest/version；
- `state_entities`：稳定实体身份；
- `state_events`：append-only 事实源；
- `state_projections`：当前投影；
- `state_snapshots`：可验证加速点；
- `state_candidates`：候选、confidence、expiry、审批状态；
- `state_conflicts`：冲突组和裁决状态；
- `state_approvals`：批准/拒绝/revoke authority；
- `state_source_anchors`：最小必要来源锚点；
- `state_verifications`：验证记录；
- `state_writer_leases`：单 writer lease；
- `delegation_contracts`：不可变 authority digest；
- `outcome_records`：reported/verified/accepted 分层；
- `state_receipts`：idempotent append receipt。

现有四库只通过不可逆向写入的外部 ID 和 digest 引用，不使用 SQLite cross-database FK。

## 12. Writer 与生产边界

首版使用本地 `state-admin` CLI，不提供 Web 写接口。

写入步骤：

1. 读取 authority artifact；
2. 校验 target project、read/write scope、prohibited operations、valid period；
3. 生成 canonical preview 和 digest；
4. 用户/本任务授权方使用同一 digest 调用 commit；
5. 获取单 writer lease；
6. `BEGIN IMMEDIATE` append event + projection + receipt；
7. 验证 event/projection/receipt digest；
8. 释放 lease。

生产写入还要求：

- 显式生产开关，默认 off；
- 固定 Operational State DB 路径；
- 生产前四库和资产备份；
- 克隆迁移/rollback/restore 已通过；
- 无竞争服务、无现存 lease；
- 每次写入有 authority artifact 和 idempotency key。

## 13. Context Packages

### Now Package

包含 `as_of`, current focus, active goals/projects, blockers, waiting, delegations,
pending decisions, recent changes, stale/conflicted, resources, constraints, source anchors,
omitted reasons, budget 和 package digest。

### Project Resume Package

包含 identity、purpose、authority baseline、current/completed phase、active work、approved/
pending decisions、blocker、waiting、delegation、accepted outcome、next approved/candidate
step、deferred、out-of-scope、prohibited actions、source anchors、freshness 和 digest。

两个包均使用 v7 成熟的 evidence-selection 原则，但只投影 Operational State 引用；不塞入
全部历史或私人正文。canonical digest 不包含生成时随机值。

## 14. Delegation Contract

不可变 contract 至少绑定：

- user intent、target project、task；
- readable/writable resources、prohibited operations；
- risk、valid period、allowed tools、acceptance criteria、rollback；
- commit/merge/tag/push/next-node permission；
- authority digest。

默认 merge/tag/push/next-node 均 false。越界时当前动作 fail-closed，写 overreach event，
Delegation 转 waiting，不更新项目完成状态。

## 15. 只读 API

正式前缀：

`/api/operational-state`

GET：

- `/status`
- `/now`
- `/projects`
- `/projects/{project_id}`
- `/projects/{project_id}/resume`
- `/waiting`
- `/decisions`
- `/delegations`
- `/conflicts`
- `/freshness`
- `/evidence/{anchor_id}`

所有响应 `Cache-Control: no-store`。普通 Web/Tailnet/移动端对该前缀的 POST/PUT/PATCH/
DELETE 一律 403。v9 首版没有浏览器写接口。

## 16. UI 信息架构

只增加：

- Now
- Projects
- Waiting
- Decisions
- Delegations

保持文档式、低饱和、大量留白和渐进披露。桌面展示 current-first；移动只读且触控区
不小于 44×44；支持键盘、focus-visible、reduced motion、Visual Viewport 和无水平溢出。

## 17. 生产迁移

1. v9-A 仅在临时库和生产克隆建立 9001。
2. v9-B/C/D/E 在临时库和克隆完成真实行为、API 和 UI。
3. v9-F 备份四库、资产和 sidecars，记录 hash/schema/logical digest/integrity/FK。
4. 独立克隆 migration rehearsal、rollback、restore、v8 回归。
5. 停止精确识别的竞争服务，确认无 lease。
6. 创建生产第五库并写入最小真实种子。
7. 恢复 real-readonly 服务，验证 Tailnet、GET-only、no-store。
8. 生成五库 release backup 并在独立路径恢复。

任何一步失败均 fail-closed 并恢复；不修改 v8 四库业务模型。

## 18. 测试计划

覆盖：

- schema/digest/drift/migration/restore；
- duplicate/replay/out-of-order/invalid/unauthorized transition；
- lease contention/crash/projection and snapshot corruption/rebuild；
- stale/supersession/conflict/revoke/clock anomaly/source loss；
- cross-database reference absence/privacy；
- Git/DB/Runtime/approved-memory adapters；
- Now/Resume determinism、budget、omission、source-backed；
- delegation/outcome/overreach；
- GET-only/no-store/mobile/PWA/visual/accessibility；
- v8 全量相关回归、compileall、JS、PowerShell AST、privacy/key scan。

## 19. v9-A 至 v9-F 路线

| 节点 | 交付 |
|---|---|
| v9-A | 9001 core、events、projection、snapshot、lease、recovery、state-admin |
| v9-B | trusted adapters、reconciliation、两个真实项目的只读种子 |
| v9-C | Now/Resume packages、budget、digest、只读 API |
| v9-D | Delegation contract、Runtime reference、Outcome loop、overreach |
| v9-E | 桌面/移动只读控制面、PWA/视觉/可访问性 |
| v9-F | 生产第五库、真实验收、恢复、全回归、main、tag、封版 |

## 20. v9.0 Gate

- 不重复 v7 Memory：通过；
- 不复制 Runtime Ledger：通过；
- 不复用 ingestion event 语义：通过；
- 不进入 v10：通过；
- 不建立第二套 evidence/authority：通过；
- 独立第五库理由与恢复边界：通过；
- 生产迁移未执行：通过；
- v9-A 至 F 路线完整：通过。

结论：`V9_0_DESIGN_BASELINE_GO`
