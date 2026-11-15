# YunOS v8.3-G0 Historical Reconstruction Schema Compatibility

## 状态

本节点是正式 v8.3-G 之前的内部 schema/authority 纠偏。本文件随 G0 正式提交关闭；不授权生产 migration、历史 backfill、writer、媒体晋升、v8.3 merge、G0 tag 或 push。本轮另行补齐的 v8.2 release annotated tag 属于已授权发布治理，不扩大 G0 执行权限。

结论性架构：

- v8301 = legacy ingestion authority compatibility；
- v8302 unified v2 chain = future single active authority；
- `SourceManifest` 与 `HistoricalSourceManifest` 只有来源证据表达不同；
- 两者进入 `ManifestSubject` 后，共用一条 batch/event/conflict/decision/approval/execution/verification/lease authority。

## 架构裁决与原 G0 分叉审计

| 原 G0 组件 | 裁决 | 修订结果 |
| --- | --- | --- |
| HistoricalSourceManifest | 保留并重构 | `archive_state=unavailable`，archive SHA/size 可空且此处必须为空，未知 adapter name/version 保持 NULL，`reconstruction_basis_digest` 必填 |
| historical source binding | 保留证据模型 | 改为引用 `manifest_subject_id` 与 `unified_batch_id` |
| historical attachment binding | 保留证据模型 | 改为引用统一 batch；metadata-only/missing/unresolved 的 `entity_digest=NULL` |
| historical batch | 重构进入统一主链 | 使用 `ingestion_batches_v2` 与 v8.3-A `ImportBatch` |
| historical batch events | 重构进入统一主链 | 使用 `ingestion_batch_events_v2` 与同一 event digest chain |
| historical conflicts/decisions | 重构进入统一主链 | 使用 `ingestion_conflicts_v2` / `ingestion_decisions_v2` |
| historical approvals | 删除独立 authority | 使用 `ingestion_approvals_v2` 与 v8.3-E exact-plan `ApprovalArtifact` validator |
| historical executions/events | 删除独立 authority | 使用 `ingestion_executions_v2` / `ingestion_execution_events_v2` 与同一 `ExecutionBundle`、journal、recovery/rollback |
| historical verifications | 删除独立 authority | 使用 `ingestion_verifications_v2` 与同一 `ExecutionVerificationResult` |
| historical lease/writer | 禁止独立实现 | 使用 `ingestion_leases_v2` 与同一 target writer lease |

活跃 v8302 schema 不创建以下被驳回的对象：

- `ingestion_historical_approvals`
- `ingestion_historical_executions`
- `ingestion_historical_verifications`
- historical 专用 batch、event、conflict、decision、execution-event 表

## ManifestSubject

`ingestion_manifest_subjects` 是来源表达与执行 authority 的唯一连接点：

- `manifest_kind=source`：只允许 `source_manifest_id` 非空；
- `manifest_kind=historical_reconstruction`：只允许 `historical_manifest_id` 非空；
- 两个引用必须严格二选一；
- `subject_digest` 由 kind、引用 ID、证据策略版本和 schema 版本确定性生成；
- digest payload 不含绝对路径、标题、正文或完整私人业务 ID。

普通 adapter 仍只能产生严格 `SourceManifest`。历史 planner 只能产生诚实的 `HistoricalSourceManifest`。未知 historical adapter 在 manifest 中保持 NULL；进入 core 时使用执行处理器标识，不把它伪装成已证实的原 adapter。

## 统一 v2 authority

v8302 新增 13 张表：

1. `ingestion_historical_manifests`
2. `ingestion_manifest_subjects`
3. `ingestion_batches_v2`
4. `ingestion_batch_events_v2`
5. `ingestion_conflicts_v2`
6. `ingestion_decisions_v2`
7. `ingestion_approvals_v2`
8. `ingestion_executions_v2`
9. `ingestion_execution_events_v2`
10. `ingestion_verifications_v2`
11. `ingestion_leases_v2`
12. `ingestion_historical_source_bindings`
13. `ingestion_historical_attachment_bindings`

它同时服务 `full`、`incremental`、`historical_backfill` 与 `verification_only`。模式差异只能由 batch mode、manifest kind、证据策略、重建依据、provenance 和 operation handler 表达；completed/failed/recovery_required/rolled_back 的定义不分历史与普通来源。

统一复用点：

- v8.3-A `create_import_batch` / `transition_batch` / `replay_batch_events`；
- v8.3-A event schema 和 digest chain；
- v8.3-B `ImportPlan`；
- v8.3-E `validate_approval_artifact`；
- v8.3-E `ExecutionBundle`、execution journal、recovery、rollback；
- v8.3-E `verify_test_execution`；
- v8.3-E `acquire_writer_lease`；
- v8.3-F clone migration marker/recovery protocol。

`execute_historical_operation_handler()` 只委托给统一 `execute_test_bundle()`，没有第二个 historical state machine。

## Historical evidence 约束

历史 manifest：

- `archive_state=unavailable`
- `archive_sha256=NULL`
- `archive_size=NULL`
- 无法证明的 adapter name/version 为 NULL
- `reconstruction_basis_digest`、`evidence_policy_version`、`provenance_kind` 必填

历史 source binding：

- `legacy_unknown` 不得生成 source ID digest；
- `probable_legacy` 只能 deferred/rejected；
- `conflicting_evidence` 必须 blocked；
- `deterministic_legacy` 才能携带可执行 source binding digest。

历史 attachment binding：

- state 为 `resolved`、`metadata_only`、`asset_record_missing`、`entity_missing` 或 `unresolved`；
- 只有 `resolved` 可携带真实 `entity_digest`，且必须携带；
- `size_conflict_unresolved` 必须保持 entityless；
- 13 条既有大小冲突不得生成 `AssetEntity`。

## 最终静态审计

对 active v8302 table inventory 和实际 `HISTORICAL_MIGRATION_DDL` payload 的静态
检查结果为 PASS：

- active tables / indexes / triggers = 13 / 13 / 31；
- `ingestion_historical_approvals`、`ingestion_historical_executions`、
  `ingestion_historical_execution_events`、`ingestion_historical_verifications`
  在 active table inventory 和 active DDL 中均为 0；
- active table inventory 精确等于 manifest、subject、两个 binding 和统一 v2
  batch/event/conflict/decision/approval/execution/verification/lease；
- `execute_historical_operation_handler()` 委托统一 `execute_test_bundle()`；
- 没有 historical 专用 approval validator、execution journal 或 verification
  engine symbol。

原始源码全文搜索仍会命中被裁决驳回的 pre-ruling DDL builder 及负向测试断言；这些
builder 被后续常量重绑定明确置为 inactive，不进入 migration digest 或执行 payload。
本轮没有为消除文本命中而改写已完成完整回归裁决的核心实现。机器可读结果保存在
ignored `output/v8.3-governance-closure/g0_static_audit.json`。

## Migration 与恢复

- v8301 DDL、digest `0000000000000000000000000000000000000000000000000000000000000000`、marker 和 schema SQL 保持不变；
- 修订后 v8302 digest：`0000000000000000000000000000000000000000000000000000000000000000`；
- 被废弃的预修订观测 digest：`0000000000000000000000000000000000000000000000000000000000000000`；
- v8302：13 tables / 13 indexes / 31 triggers；
- 迁移严格加法式，生产入口继续 hard-refuse。

生产只读 online-backup 克隆通过：

- v8301 marker/digest/schema 保持；
- v8302 marker chain 通过；
- integrity check `ok`，foreign key violations `0`；
- legacy IDs、source、attachments、FTS、desktop/v8.2 read contracts 不变；
- ordinary 与 historical subject 各进入一条统一 batch；
- 两者共用 v2 batch/event/approval/execution/verification/lease 表；
- metadata-only entity digest 为 NULL；
- append-only trigger 拒绝 UPDATE；
- exact replay=`already_applied`；
- verified backup restore 回到 v8301。

## 验收与停止点

专项 52 项通过，包含 subject 二选一、普通 archive 严格性、historical nullable archive/entity、统一状态机/event/approval/execution/verification/lease、故障注入、schema drift、digest mismatch、unknown version、restore 和 production refusal。

A–F、媒体、ChatGPT/DeepSeek 与 v8.2 核心定向回归通过 478 项（1 项既有 skip）。实现冻结后执行的本轮唯一一次完整回归通过 1,269 项（5 项既有 skip），0 failure/error，用时 153.856 秒。

治理结论见：

- `docs/governance/yunos_production_baseline_audit.md`
- `docs/governance/yunos_v8_2_release_governance_audit.md`
- `docs/governance/yunos_stash_and_ignore_audit.md`

本节点以精确暂存和单一 G0 提交关闭；提交完成后仍停留在
`codex/v8.3-g0-historical-schema-compatibility`，不进入正式 v8.3-G。v8.2 的
local main/tag/catch-up backup 治理闭环不代表远端已 push，也不授权任何生产执行。
