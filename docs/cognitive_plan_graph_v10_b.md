# YunOS v10-B Plan Graph 核心

## 1. 范围与停止点

Plan 核心提供纯内存、不可变、可验证的 Plan Graph 合约与算法；独立
`CognitivePlanningStore` 只把已验证 Graph 原子写入第五个 Operational State DB。两者都
不执行工具、不修改 v7 长期记忆，也不授予任何执行权限。它实现：

- `CapabilityRequirement`、`RetryPolicy`、`PlanNode`、`PlanEdge`、
  `PlanRevision`、`PlanGraph` 的闭合结构；
- 顺序、并行、决策、人工门禁、替代路径、回滚、暂停和恢复；
- dependencies/edges 一致性、拓扑排序、DAG 和环检测；
- deterministic plan/revision digest、parent digest 修订链；
- plan comparison、feasibility、scope/risk/capability expansion 检查；
- budget、secret、prompt-injection、下一版本和 release operation 的
  fail-closed 拒绝；
- 仅供展示的 `WorkflowCandidate`，不提供保存或长期记忆写入路径。

Plan 核心不是 Tool Gateway、Authorization Artifact 签发器或执行器。Store 也只是
authority/lease/event/receipt 门禁后的 append-only persistence；这些对象都不能由 Plan
Graph 推断或替代。

## 2. Digest 投影与无自引用保证

### 2.1 Plan digest

`plan_digest = SHA-256(canonical_json(plan_identity_payload))`。

`plan_identity_payload` 精确包含：

- `plan_id`
- `objective_id`
- `objective_version`
- `objective_digest`
- canonical ordered `nodes`
- canonical ordered `edges`
- `entry_node_ids`
- `terminal_node_ids`
- `version`

它明确排除：

- `plan_digest` 自身；
- 整个 `revisions` 集合；
- node/edge 的派生 digest；
- 任何运行时状态、授权 artifact、tool result 或 persistence metadata。

因此 embedded `revision.plan_digest` 不会参与 Plan digest 计算，不存在
Plan → Revision → Plan 的 digest 自引用。相同 NFC 文本、集合语义输入和图结构产生相同
plan ID 与 plan digest；revision reason 或 created time 不改变同版本图身份。

### 2.2 Revision digest

Revision 的闭合 JSON 字段为：

- `revision_id`
- `plan_id`
- `from_version`
- `to_version`
- `change_reason`
- `authority_impact`
- `previous_digest`
- `plan_digest`
- `revision_digest`
- `created_at`
- `created_by`

`revision_digest` 的 identity projection 只排除 `revision_digest` 自身。初始 revision 固定
`from_version=null`、`to_version=1`、`previous_digest=null`；后续 revision 必须满足
`to_version=from_version+1`，且 `previous_digest` 精确等于父 Plan digest。

该结构与 SQL blueprint 的 `from_version`、`to_version`、`plan_digest`、
`revision_digest` 语义对齐；`created_by` 是 closed JSON 审计字段，不是 authority。

## 3. Capability 绑定

Plan Node 不接受裸 capability ID。每项 capability requirement 必须同时绑定：

```json
{
  "capability_id": "capability.inspect",
  "capability_version": 1,
  "capability_digest": "<64 lowercase hex>"
}
```

相同 `(capability_id, capability_version)` 不得重复。Prepare/Act Objective 的 requirement
必须位于其 `authority_request.capabilities`；Plan Agency 可以描述尚未授权的未来
capability requirement，但只能得到 `waiting_authority`/不可执行结论。持久化前 Store 会
在同一事务内对 DB Registry 重新核对 ID、version、digest。真正执行前 Tool Gateway 还必须
复核 availability、scope、risk 和 Authorization Artifact。Plan 中存在 binding 不代表
capability 可用，更不代表允许执行。

## 4. Node 合约

每个 Node 必须显式表达：

- identity、objective binding、title、purpose、dependencies、node kind；
- capability ID/version/digest requirements；
- read scope、write scope、risk、reversibility、authority requirement；
- 至少一个 success criterion 和 verification；
- rollback、timeout、最多两次 attempt、最多一次 equivalent retry；
- state、source-anchor evidence、omitted reason。

约束包括：

- `omitted` 必须有 `omitted_reason`，非 `omitted` 不得伪带理由；
- reversible write 必须声明 rollback；
- irreversible node 只能要求 `explicit_act` 或 `human_gate`；
- human gate 必须使用 `authority_requirement=human_gate`；
- rollback node 必须声明 rollback steps；
- timeout 固定在 `1..1,200,000 ms`；
- retry `maximum_attempts <= 2` 且 `equivalent_retry_limit <= 1`。

## 5. Edge、dependencies 与 DAG

Edge 支持：

- `sequence`
- `parallel`
- `decision`
- `failure`
- `rollback`
- `alternative`
- `resume`

每一条 edge 的 source 必须出现在 target 的 `dependencies` 中；每个 Node 的
`dependencies` 也必须与所有 incoming edge source **精确相等**。缺边、多边、悬空节点、
self-loop、重复 route 或不一致都失败。

结构约束：

- `decision` edge 必须从 decision node 发出，至少两个分支且每条有 condition；
- `parallel` 至少两个同源分支；
- `rollback` edge 必须指向 rollback node；
- `alternative` edge 必须指向 alternative node；
- `resume` edge 必须从 human gate 发出；
- 同一 source/kind 的 priority 唯一；
- entry/terminal 集合必须等于真实 zero-indegree/zero-outdegree 集合；
- Kahn 拓扑检查必须访问全部节点，否则 `plan_cycle_detected`。

## 6. Revision、暂停与恢复

Plan 不原地修改。`pause_plan` 把允许的节点状态修订为 `waiting_external`；
`resume_plan` 只恢复 `waiting_external`/`waiting_authority`：普通节点回到 `ready`，已经取得
显式决定的 human gate 进入 `completed`。两者都：

1. 创建下一 Plan version；
2. 重新计算 Plan digest；
3. 写入 change reason；
4. 绑定 parent Plan digest；
5. 生成独立 revision digest。

无节点或 edge 语义变化的 revision 被视为无进展，返回
`plan_revision_no_progress`，防止无限 replan。

## 7. Plan comparison 与 authority impact

Comparison 输出 added/removed/changed nodes、edges、capabilities、read scopes、write
scopes，以及 risk increase：

- 新 node、scope、capability 或更高风险：
  `authority_impact=requires_new_authority`；
- 只有移除或收窄：`authority_impact=narrows_scope`；
- 仅状态、验证文字或 edge 调整：`authority_impact=none`。

纯模型只标记 `requires_new_authority`，不提供 boolean 或 agent-generated approval bypass。
Store 只有在收到独立 `user:*` authority、`plan_scope_expansion_approved` 事件、writer lease
和精确 parent digest 后才可持久化该 revision；这仍不是 Act Authorization Artifact，
也不允许执行节点。

修改 `objective_id`、`objective_version` 或 `objective_digest` 属于根目标变化，始终拒绝。

## 8. Feasibility

`assess_plan_feasibility` 是只读判断，不改变状态。它检查：

- graph 与 Objective 的 ID/version/digest 绑定；
- Objective 是否仍有高影响歧义；
- node authority requirement 是否超过当前 agency level；
- waiting state；
- irreversible node；
- consult/plan 下的 write execution。

返回的 `feasible=true` 只表示静态 Plan 合约没有已知阻断，不代表工具可用、Authority
Artifact 有效、系统已验证或用户已接受。

## 9. Fail-closed 边界

### 9.1 Scope 和 release

- scope 禁止 wildcard、`..` traversal、NUL 和多行文本；
- node scope 必须位于 Objective roots 内；Plan Agency 的 prospective write scope 只可位于
 既有 read root 内并强制 `explicit_act`/`human_gate`；
- Prepare/Act capability 必须位于 Objective requested capability IDs；Plan Agency 只可描述
 未来 requirement，不能激活；
- risk 不得超过 Objective ceiling；Plan Agency 使用 Objective risk tolerance；
- `merge`、`tag`、`push` capability/action 永久拒绝；
- `next-version`、`v10-G`、`G0`、`v10.1`、`v11` 创建永久拒绝。

### 9.2 Secret 和 prompt injection

所有控制文本、scope、capability ID、condition、evidence locator 会经过：

- 常见 API key、GitHub token、AWS key、Bearer、JWT、private key 检测；
- `ignore previous instructions`、system/developer prompt 泄漏、role token、
  jailbreak directive 检测。

命中后内容不能进入 Plan Graph。Evidence 仍然只是 source-anchored data，永远不产生
authority。

### 9.3 Hard budgets

默认上限：

- 128 nodes；
- 512 edges；
- 64 revisions；
- 1 MiB canonical envelope；
- 512 evidence items；
- 12 小时累计 node timeout。

调用方可以进一步收紧，不能超过模块绝对上限（512 nodes、2048 edges、128 revisions、
4 MiB、2048 evidence、24 小时 timeout）。达到上限即停止，不自动扩大预算。

## 10. Workflow Candidate

`propose_workflow_candidate` 只返回 immutable、in-memory candidate：

- `candidate_only=true`
- `writes_long_term_memory=false`

对象没有 `save`/`persist` 方法，也不接触 v7 profile、approved memory 或数据库。一次计划的
成功或失败不能直接改写长期画像；后续若需要学习，必须走独立的 candidate、多次证据和用户
批准链路。

## 11. 同库持久化与只读 API

`memory/cognitive_planning_store.py` 复用 v9 `StateEvent`、receipt、user authority 与 writer
lease，在单一 `BEGIN IMMEDIATE` 内写入：

- `plan_graphs`
- 当前版本 `plan_nodes`
- 当前版本 `plan_edges`
- 当前 `plan_revision`
- 对应 v9 event/receipt

Store 在事务内重读 Objective ID/version/digest、Capability ID/version/digest、parent
revision、authority 和 lease；任何失败整体 rollback。Graph、node、edge、revision 读取时
从分表重建并重算全部 digest，坏行不会以部分可信结果返回。revision 的持久时间由 trusted
service clock 重建，不能由请求伪造。

普通 Web 只增加：

- `GET /api/cognitive/plans`
- `GET /api/cognitive/plan`

响应固定 `readonly=true` 与 `Cache-Control: no-store`；没有 Plan/Capability POST，Search
route 未导入 planner/provider。

## 12. 独立验收

独立测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_v10_planning tests.test_v10_planning_store tests.test_v10_planning_api -v
```

验收覆盖 closed round-trip、deterministic digest、digest tamper、所有 control-flow edge、
dependencies/edges、cycle、comparison、revision parent chain、scope/risk/capability denial、
暂停恢复、secret/injection、retry/budget、release/next-version denial 和 candidate-only
learning boundary。
