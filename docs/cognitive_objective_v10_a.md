# YunOS v10-A Objective and Intent Engine

## 1. 定位

v10-A 是 YunOS v10 — 受限认知代理的第一个可执行节点。它把用户输入整理为
`Objective Contract`，但不生成 `Plan Graph`、不调用 Provider、不调用工具，也不授予执行
权限。

本节点遵守以下分层：

- v7 继续提供长期记忆证据；
- v8 继续拥有模型执行、receipt、usage、artifact 与 ledger；
- v9 继续拥有 Operational State、Source Anchor、State Event、candidate、approval、
  writer lease 与 receipt；
- v10-A 只新增 Objective/Intent 合约与同库加法式存储。

搜索模式没有接入 Objective Engine，仍只走本地 SQLite/FTS。

## 2. Objective Contract

正式实现位于 `memory/cognitive_objective.py`，输出字段与
`docs/schemas/yunos_v10_cognitive_agency.schema.json` 对齐：

- `objective_id`
- `user_utterance`
- `interpreted_goal`
- `project_id`
- `agency_level`
- `agency_selection`
- `cognitive_stance`
- `success_criteria`
- `prohibited_outcomes`
- `risk_tolerance`
- `time_constraints`
- `resource_constraints`
- `assumptions`
- `ambiguities`
- `clarification_required`
- `context_requirements`
- `acceptance_policy`
- `authority_request`
- `source_anchors`
- `canonical_digest`
- `version`

Objective 文本字段在建模时进行 NFC 规范化；集合语义字段排序、去重后再进入 canonical
JSON。复用的 v9 `SourceAnchor` 是 opaque stable reference：v10 只校验 locator 的非空、
长度、NUL 与 secret 边界，不 trim、不改写 locator，也不在同一 `anchor_id` 下改变其
canonical content。既有 canonical JSON 层仍执行 NFC，因此 composed/decomposed locator
得到相同确定性 digest，同时首尾空白继续属于 locator 自身语义。`canonical_digest` 为排除
自身后的 SHA-256。未知字段、非有限 JSON、无成功标准、无来源、错误枚举、错误时间或
digest 不匹配均 fail-closed。

Objective 输入限制为 1 MiB JSON、单个集合最多 256 项、source anchor 最多 64 项、
canonical contract 最多 256 KiB。API key、Bearer token、password assignment、private key
等 secret literal 在形成 preview、写入数据库或进入 GET 响应前统一 fail-closed；只允许保存
稳定 `secret_reference`，不允许保存凭据值。

`Objective Contract` 永远不等于 `Authorization Artifact`。即使 `agency_level=act`，
它也只表达 authority request，不能自行执行。

`AcceptancePolicy` 默认要求显式用户事件。自动接受只有在独立预授权 source anchor/digest、
低风险、确定性标准、系统验证、可逆、无冲突、无越权和无未解决警告全部成立时才可表达；
普通 CLI/model 输入不能自报自动接受。

## 3. Agency Level

| 内部值 | 中文 | v10-A 行为 |
| --- | --- | --- |
| `consult` | 讨论 | 只形成分析合约；写范围、命令、capability 与写权限全部清空，禁止 candidate 或 authoritative persistence |
| `plan` | 规划 | 形成正式 Objective；不执行、不写项目 |
| `prepare` | 准备 | 可记录后续授权需求，但 request 不授予权限；本节点仍不执行工具 |
| `act` | 执行 | 只形成显式 authority request；没有 Authorization Artifact 就不能执行 |

选择来源只允许：

- `default`：只能得到 `consult`；
- `user`：用户明确选择；
- `preauthorized_rule`：已存在的受控规则。

选择结果以 closed `agency_selection` 持久化，绑定可信 SourceAnchor ID、evidence digest 与
selection digest。Agent proposal 只能处于用户已选级别之内，不能提升级别。Cognitive
Stance 不参与权限计算。

## 4. Cognitive Stance

| 内部值 | 中文 | 作用 |
| --- | --- | --- |
| `continue` | 延续 | 优先复用已验证方法 |
| `explore` | 探索 | 允许比较新方法 |
| `breakthrough` | 突破 | 降低历史惯性、重新比较路径 |

Stance 只进入 Objective 的方案偏好，不增加读写范围、命令、网络、数据库或 Git 权限。

## 5. 澄清策略

v10-A 接收结构化 ambiguity signal，不通过自然语言关键词猜测授权。以下 signal 必须澄清：

- `material_goal_change`
- `material_risk_change`
- `irreversible_operation`
- `new_write_scope`
- `account_action`
- `money_action`
- `external_publication`
- `major_value_conflict`
- `evidence_conflict`
- `missing_success_criteria`

以下情况记录为低影响检查，不中断用户：

- `required_project_read`
- `existing_test_run`
- `git_status_check`
- `obvious_local_fix`
- `verification_report`
- `authorized_reversible_step`

存在硬歧义时：

- `clarification_required=true`；
- 可继续只读工作；
- 所有写 authority request 被清空；
- 只能创建 candidate；
- candidate 获批也不会自动成为 Authorization Artifact；
- 未形成无歧义 Contract 前不得 authoritative apply。

## 6. 上下文预算

默认上下文只声明最小必要来源：

- v7 Memory Package；
- v9 Now Package；
- v9 Resume Package；
- 当前 project；
- Runtime stable references。

每项声明 `maximum_items`、`maximum_bytes` 和用途。总预算上限为 64 项、131072 bytes。
Objective 只保存稳定 source anchor 与预算，不保存全量数据库或无关私人状态。

## 7. 同库 schema generation 10001

实现位于：

- `memory/cognitive_state_migration.py`
- `memory/cognitive_state_store.py`

本节点只对临时数据库或后续克隆开放 migration；生产路径明确拒绝。它在第五个
Operational State DB 内新增：

- `cognitive_schema`
- `objective_contracts`
- `provider_registry`
- `capability_registry`
- `plan_graphs`
- `plan_nodes`
- `plan_edges`
- `plan_revisions`
- `context_bindings`
- `authorization_artifacts`
- `cognitive_runs`
- `cognitive_run_checkpoints`
- `tool_proposals`
- `tool_executions`

这是一次完整、唯一的 generation 10001 schema foundation：14 tables、14 immutable
triggers、7 indexes，共 35 个对象。v10-A 只激活 Objective 行为；其余表在后续节点按既定
契约启用，不再改变 generation 或追加第二次 v10 初始 migration。

保留：

- `operational_schema.marker=yunos-operational-state-9001`
- `operational_schema.schema_version=9001`
- 既有 `StateEvent.schema_version=9001`

新增：

- `cognitive_schema.marker=yunos-bounded-cognitive-agency-10001`
- `cognitive_schema.schema_generation=10001`

migration 使用单一 `BEGIN IMMEDIATE`：

1. 对目标执行 lexical/real/file identity 与 SQLite open 后 identity 复核；
2. 确认 writer lease 为零；
3. 检查 partial 10001 schema；
4. 验证 v9 full schema fingerprint；
5. 创建精确 allowlist 的 35 个 extension objects；
6. 重算全库 schema fingerprint；
7. 同步更新 `operational_schema.schema_digest`；
8. 写入 `cognitive_schema`；
9. 验证双 marker、完整对象集和相同 digest；
10. commit；任何异常 rollback。

`objective_contracts` 为 append-only。实现相对 v10.0 blueprint 增加了
`approval_id -> state_approvals` 稳定绑定，并把 `created_event_id` 设为唯一；这是为了阻断
approval 跨 Objective 重绑并保证一条 Objective revision 只绑定一个审计事件，已同步回
SQL blueprint。

## 8. 原子提交

v9 `OperationalStateStore.commit_preview()` 被最小重构为：

- 对外 public API 行为不变；
- 内部增加 connection-scoped private primitive；
- v10 Store 可在同一事务中写：
  - v9 Source Anchor；
  - v9 State Event；
  - v9 projection；
  - v9 receipt；
  - v10 Objective row。

因此不会出现“有事件、无 Objective”或“有 Objective、无 receipt”的半完成状态。重复
`idempotency_key` 只在 event、authority、preview 完全一致时返回原 receipt；碰撞会拒绝。

## 9. Candidate 和 Approval

歧义 Contract 复用：

- `state_candidates`
- `state_approvals`
- v9 authority
- v9 writer lease

安全规则：

- candidate create 必须由 `user:*` authority 发起，Agent/System 不得创建或批准 Objective
  candidate；
- candidate identity 绑定完整 Objective digest、confidence、SourceAnchor digest、expiry、
  project/entity/type；同 ID replay 必须逐项一致，过期 replay fail-closed；
- candidate digest 必须与确认值一致，完整记录在同一 `BEGIN IMMEDIATE` 决策事务内再次
  校验，验证失败不会生成 approval 或改变 candidate 状态；
- candidate create/decision 必须持有匹配 authority 与 active writer lease；
- authority、writer lease 与 candidate expiry 一律使用可信服务时钟判断；请求中的历史时间不能
  复活已过期权限；
- candidate 过期边界在同一 `BEGIN IMMEDIATE` 内检查；
- approval 必须为 `approve` 且 candidate 仍为 `approved`；
- approval 必须来自与 Objective commit 精确相同的 `user:*` authority，event actor 必须与
  authority issuer 完全一致；
- candidate type 必须为 `objective_contract`；
- approval 必须与最终 Contract 的完整 canonical JSON/digest、project、version 与 source
  anchor 精确一致；澄清后的 Contract 必须另建 candidate 并重新审批；
- 撤销 approval 后不得授权新的 Objective commit；已经提交的 Objective 仍可作为审计历史
  读取，并显式返回当前 approval 状态；
- Objective commit 的 `StateEvent.recorded_at` 与 Objective `created_at` 使用可信服务时钟，
  请求时间只作输入校验，不能伪造持久时间；
- approval 不授予工具、网络、Git、数据库或长期记忆权限；
- 有歧义的 Contract 即使获批也不能直接 authoritative apply，必须形成已澄清 Contract。

## 10. API 和本地控制面

### 普通 Web

只新增三个 GET：

- `/api/cognitive/objectives`
- `/api/cognitive/objective`
- `/api/cognitive/objective-candidates`

所有响应显式包含 `readonly=true`，HTTP 使用 `Cache-Control: no-store`。query 参数使用严格
白名单。没有 Objective POST route，也没有 readonly POST 豁免，因此普通 Web、Tailnet 与
移动端不能创建、批准或应用 Objective。

Objective candidate GET 使用专用、限量 SQL，只读取 `candidate_type=objective_contract`，
并逐项验证 canonical byte budget、Contract schema/digest、确定性 ID、project 与 SourceAnchor
绑定；共享 v9 candidate 既不会被全量装载，也不能把未验证载荷带入响应。严格 query parsing
只作用于 `/api/cognitive/*`，既有 Search/Runtime/移动 API 保持原解析契约。

### 本地 CLI

入口：

```text
.\run.ps1 objective-admin <subcommand> [args]
```

子命令：

- `resolve`
- `preview`
- `status`
- `list`
- `show`
- `candidate-create`
- `candidate-decide`
- `apply`

正文不进入命令行。只读 `resolve/preview` 可从 UTF-8 JSON 文件或 stdin 读取；Windows
PowerShell 5 为避免管道编码损坏应优先使用文件。所有写子命令强制要求 UTF-8 JSON 文件，
拒绝 stdin。写操作还要求：

- 临时/克隆 Operational DB；
- preview digest；
- `AuthorityArtifact` JSON；
- lease owner；
- lease token file。

lease token 本身不进入 process command line。v10-A 不提供生产 migration 开关。
JSON 与 token 都以 binary `read(max+1)` 实施实际读取上限，再执行严格 UTF-8-SIG 解码；
不是读取完整文件后才检查大小。

## 11. 已验证边界

- Objective digest 对 NFC 和集合输入顺序稳定；
- v9 `SourceAnchor` locator 保持 opaque 原值；v9 已持久化 anchor 可由 v10 重用且不会发生
  `source_anchor_identity_collision`；
- default Agency 不能高于 consult；
- Agent 不能提升 Agency；
- Agency selection 与可信 source anchor/digest 绑定；
- prompt injection 文本只作为 utterance，不变成 authority；
- secret literal 在 preview/DB/API 前拒绝且错误不回显原值；
- Consult/Plan 清空写需求；Prepare 只记录需求而不授予权限；
- Consult 不得创建 candidate 或 authoritative persistence；
- 自动接受必须绑定独立预授权并满足全部低风险门禁；
- Cognitive Stance 不扩大权限；
- success criteria 缺失进入硬澄清；
- candidate approval 不能跨 Objective 重绑；
- shared v9 candidate/approval 旁路、非用户批准与伪造 Objective candidate GET 均 fail-closed；
- 历史请求时间不能复活 expired authority/lease；
- candidate 过期、authority/lease 缺失与决定后 replay 均 fail-closed；
- candidate identity/replay 绑定 confidence、expiry、完整 Contract 与 SourceAnchor；
- candidate 决策在事务内重新校验，篡改记录时 approval/status 零变化；
- approval revoke 后历史 Objective 可读，但撤销状态不能授权新 commit；
- event/objective 持久时间由 trusted clock 提供；
- migration failure 原子回滚；
- active writer lease 阻断 migration；
- production DB target、hardlink alias 与 validate/open replacement 被拒绝；
- Objective/Event/Receipt 原子且幂等；
- normal Web 没有 Objective POST；
- Search route 未改变；
- Provider calls 为零；
- v9 core/acquisition/context/delegation 回归通过。

## 12. 明确未实现

v10-A 没有实现：

- Plan Graph；
- Capability Registry；
- Provider Registry 或真实模型调用；
- Tool Gateway；
- Authorization Artifact 执行验证；
- Agent Loop；
- Supervision/Recovery；
- 认知任务工作区 UI；
- 生产 Operational migration；
- v10-B 之后的任何运行能力。
