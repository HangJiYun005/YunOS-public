# ADR-001：在既有 Operational State 中增加 Bounded Cognitive Agency Extension

状态：Accepted for v10.0 design gate

日期：2026-07-31

范围：YunOS v10 — Bounded Cognitive Agency

## Context

YunOS v7 已提供长期记忆、证据选择和上下文装配；v8 已提供固定 Provider 调用、
request artifact、execution、usage、receipt 和 ledger；v9 已提供第五个 Operational
State DB、StateEvent、projection、Delegation、Outcome、Verification、Acceptance、
Writer Lease、Now Package 和 Project Resume Package。

v10 需要表达 Objective Contract、Cognitive Run、Plan Graph、Provider Registry、
Capability Registry、Authorization Artifact、Tool Proposal 和 Tool Execution。它同时
必须满足：

- 不创建第六个数据库；
- 不复制 v7 Evidence；
- 不创建第二套 v8 Runtime/Receipt/Ledger；
- 不复制 v9 StateEvent、Approval、Delegation、Outcome 或 Writer Lease；
- 普通 Web、Tailnet 和移动端保持只读；
- 生产写边界必须可以独立证明；
- v9 既有 core 与历史记录不可重写。

现有 Operational State 有两个重要约束：

1. `operational_schema.schema_version` 与 marker 固定为 `9001`；
2. `StateEvent.schema_version` 和表级 CHECK 也固定为 `9001`。

因此把全局常量直接改成 `10001` 会破坏既有事件读取，并要求 rebuild 旧表，不符合加法式
迁移要求。

## Decision

### 1. 同一 Operational State DB

v10 继续使用：

`data/operational/ayunos_operational_state.db`

不创建第六个数据库。v10 表和 v9 core 共享同一备份、restore、writer lease、生产写门禁
和 schema drift 审计。

### 2. 双 generation marker

- 保留 v9 `operational_schema` marker 与 core contract `9001`；
- 保留所有既有 `StateEvent.schema_version=9001`；
- 新增 `cognitive_schema`；
- v10 extension marker 固定为
  `yunos-bounded-cognitive-agency-10001`；
- extension generation 固定为 `10001`。

一次受控 migration 在同一事务中创建 extension 表、索引和 append-only triggers，写入
`cognitive_schema`，重算全库 schema fingerprint，并更新既有
`operational_schema.schema_digest`。不 rebuild、不删除、不重写旧表或旧事件。

### 3. 最小新增结构

新增：

- `objective_contracts`
- `cognitive_runs`
- `plan_graphs`
- `plan_nodes`
- `plan_edges`
- `provider_registry`
- `capability_registry`
- `context_bindings`
- `authorization_artifacts`
- `tool_proposals`
- `tool_executions`

复用：

- supervision audit → `state_events`
- verification → `state_verifications`
- learning candidates → `state_candidates`
- user approval → `state_approvals`
- project execution scope → `delegation_contracts`
- outcome/acceptance → `outcome_records` 与既有 acceptance loop
- state commit receipt → `state_receipts`

`tool_executions` 仅记录本地 typed capability 的不可变终态 receipt，不记录模型调用。
`queued`、`running` 与 `suspected_stall` 过程状态进入 `cognitive_run_checkpoints` 和复用的
`state_events` supervision stream；Provider model run 继续只存在于 Runtime ledger。策略在
argv/path 解析前拒绝时，receipt 必须保存空 argv 和 null working directory，不伪造解析结果。

### 4. Runtime 必须复用

所有真实模型调用继续使用 v8 Runtime 的 reviewed binding、artifact、execution、run
events、usage、receipt、failure state 和 idempotency。v10 只在 Operational State 保存
稳定 `runtime_run_id`、artifact/receipt digest 和 context binding。

Provider Registry 是编排和显示层，不是第二套 transport 或 ledger。当前生产 Runtime
固定 DeepSeek，因此 v10 首版：

- DeepSeek 做真实调用；
- Kimi-compatible 完成 profile、adapter contract 和 mock；
- 无 Kimi 凭据时标记 unavailable；
- 不把 Kimi 结果伪装为 DeepSeek；
- 不进行隐藏 Provider 切换。

### 5. local broker 优先

项目写入优先通过默认关闭的 local broker 或本地 CLI：

- 仅 loopback；
- 与 `127.0.0.1:8765` 的 `real-readonly` 服务使用不同端口/进程；
- 不配置到 Tailscale Serve；
- 使用短期 capability、独立 session、CSRF、Host/Origin 校验；
- 任务结束后关闭。

若不能证明 Web 写入口不被 Tailnet 访问，则不开放 Web writer，保留 CLI/broker。
remote IP、User-Agent、隐藏按钮、前端变量或单一 Origin 不构成授权。

### 6. Authorization 与现有 Delegation 绑定

v10 新增更完整的 Authorization Artifact，以表达 objective/plan digest、root scopes、
capability/command allowlist、risk、time/step/model/token/cost、Git/network/database 和
acceptance policy。

它必须派生或绑定现有 v9 `AuthorityArtifact` 与 `DelegationContract`。它不是第二套
Approval；用户接受继续使用 v9 approval/outcome 流程。

### 7. 不扩展 v9 entity enum

Objective 绑定既有 project/work item；Act run 绑定 delegation。Cognitive Run 的
supervision event 使用对应 work item/delegation 的 v9 StateEvent，详细 checkpoint 保存在
v10 extension 表。这样不修改 v9 entity enum 和 StateEvent lifecycle contract。

## Migration Invariants

生产 migration 必须满足：

1. 第零门禁和 v10-A 至 v10-E 全部通过；
2. 五库、sidecars、assets 已备份且可恢复；
3. 竞争服务已停止；
4. writer lease 为零；
5. 源 DB marker、schema digest、integrity、FK 与逻辑摘要匹配；
6. disposable clone migration、failure injection、rollback、restore 已通过；
7. migration 单事务执行；
8. 任一步失败都不留下半迁移 marker；
9. 迁移后 v9 core 仍可读、旧 projection 可重建；
10. writer 关闭，生产恢复 `real-readonly`。

## Rejected Alternatives

### 新建第六个 Cognitive Agency DB

拒绝。它会分裂 Objective、Delegation、Outcome、Acceptance 和 recovery 边界，引入新的
跨库一致性问题。

### 把 v10 表放入 Runtime DB

拒绝。Runtime 只负责模型执行可信账本，不负责用户目标、计划、授权或接受。

### 把 Objective/Plan 写入 Main 或 Memory DB

拒绝。当前任务状态不是长期记忆，不能污染历史聊天或 approved profile。

### 全局把 schema version 从 9001 改成 10001

拒绝。它会使旧 StateEvent 与 SQL CHECK 失效，需要 rebuild 或改写历史。

### 新建第二套 Event、Approval、Delegation、Outcome 或 Verification

拒绝。v9 已提供相应语义和安全边界，v10 只增加专属 payload 与稳定引用。

### 让 Provider 直接执行工具

拒绝。Provider output 不可信且不具备授权。Tool Gateway 是唯一执行入口。

### 把现有 runtime-admin 直接暴露为 Agency writer

拒绝。现有模式不足以单独证明 CSRF 与 Tailnet 隔离；必须使用独立 broker 或完成更强
边界证明。

### Codex 不可用时伪造 Adapter

拒绝。仅允许真实受支持接口，无法调用时标记 unavailable/handoff-only。

## Consequences

### Positive

- 不增加生产数据库数量；
- v7/v8/v9 权责保持清晰；
- v10 migration 可单独备份、回滚和恢复；
- 模型调用仍有统一 receipt/usage/ledger；
- Agent overreach、verification 和 acceptance 复用已验证实现；
- 普通 Web、移动端和 Tailnet 的只读边界不变；
- future Provider adapter 可以在不改变记忆和权限模型的前提下增加。

### Costs

- v10 代码必须同时验证 core marker 9001 和 extension marker 10001；
- schema fingerprint migration 需要严格事务和 restore rehearsal；
- Authorization Artifact 与 v9 Authority/Delegation 之间需要确定性映射；
- local broker 带来独立启动、session、CSRF 和关闭流程；
- 当前 Runtime 固定 DeepSeek，真实新 Provider 仍需未来显式 Runtime 演进。

### Risks

- 错误地把 extension generation 用作 StateEvent schema version；
- Tool execution 被误记为 model execution；
- broker 被错误配置为 Tailscale Serve target；
- Provider Registry 被误当授权来源；
- Cognitive Run 恢复重复执行工具；
- Outcome verified 被错误显示成 user accepted。

这些风险必须由 migration contract、typed executor、route audit、idempotency 和
acceptance invariant 测试覆盖。

## Validation Required Before Accepted

- 设计文档、ADR、JSON Schema 与 DESIGN ONLY SQL 一致；
- v9 core 9001 与 extension 10001 的 migration/rollback fixture；
- no-sixth-DB 与 no-second-ledger 静态审计；
- Search/AI 零串线；
- local broker/Tailnet/CSRF 边界测试；
- overreach、path、command、replay、expiry、recovery 和 acceptance 测试；
- 最终全量回归零 failed/errors。

v10.0 静态 Gate 已以 20/20 设计契约测试、可解析的 DESIGN ONLY SQL、35/35 威胁控制、
完整候选文件空白校验和变更范围 secret/privacy scan 通过。本 ADR 因而进入 Accepted；它仍
不授权任何 production migration。
