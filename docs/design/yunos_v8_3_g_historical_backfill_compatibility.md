# YunOS v8.3-G Historical Backfill and Compatibility

## 1. 节点边界

本节点只关闭历史来源证据审计、确定性回填计划、可抛弃克隆执行、兼容性、
幂等和恢复演练。生产 Main、WAL、SHM、Profile、Semantic、Runtime、
`imports/chatgpt` 和 `stash@{0}` 均为只读门禁；本节点不授权生产 migration、
historical backfill、writer、资产晋升、push、merge、tag 或 v8.3-H。

执行器只允许写入 v8302 active ingestion 表。SQLite authorizer 对旧业务表及
inactive authority 表实行 fail-closed deny-write。

## 2. 权威生产基线

权威代码基线为 G0 commit
`a7d643ab1f9993b98e9d46374a0062af4f2e1eeb`。固定 migration chain 为：

- v8301：version `8301`，digest
  `0000000000000000000000000000000000000000000000000000000000000000`；
- v8302：version `8302`，digest
  `0000000000000000000000000000000000000000000000000000000000000000`；
- v8302 active inventory：13 tables、13 indexes、31 triggers。

生产数据指纹、四库 integrity/FK、WAL/SHM、imports tree 和 stash 的测试前后值
由 ignored evidence
`output/v8.3-g-historical-backfill-compatibility/production_snapshot_summary.json`
记录。

## 3. 真实历史证据审计

审计入口以 `mode=ro&immutable=1` 读取当前生产 TargetSnapshot，并只使用已批准的
导入报告、历史快照报告、版本报告、Adapter identity policy、E4-1A media identity
审计和 G0 基线验收摘要。证据源只以 SHA-256 进入
`reconstruction_basis_digest`；路径、标题、正文、URL 和完整业务记录 ID 均不进入
公开摘要。

当前脱敏聚合审计结果为 426 conversations、16,840 messages、1,138 media
occurrences、1,133 logical media identities、0 entity available 和 13 unresolved
size conflicts。最终重算值及 source distribution 见 `evidence_summary.json`。

## 4. Evidence classification

分类词汇表严格关闭为七种：

- `native_authoritative`：现有合法 source 与稳定身份关系，无冲突；
- `deterministic_legacy`：由不可变报告、结构关系和稳定 ID 唯一证明；
- `probable_legacy`：强证据但不唯一，默认 deferred；
- `legacy_unknown`：无法证明，不生成假 ID 或 binding；
- `conflicting_evidence`：证据互斥，写 conflict ledger 并阻断 binding；
- `unsupported_source`：来源未进入支持集合，fail-closed；
- `invalid_relationship`：父子或 occurrence 关系不成立，fail-closed。

实现不使用数值 confidence，也不以标题相似、正文相似、时间 cutoff、数组顺序、
本机路径或随机 UUID 作为身份。

## 5. HistoricalSourceManifest

G0 `HistoricalSourceManifest` 被直接复用。重建 manifest 固定
`archive_state=unavailable`，archive SHA/size 为 `NULL`，不能把
`reconstruction_basis_digest` 冒充 archive SHA。`record_counts`、
`asset_counts` 和 warnings 均为脱敏聚合或 reason code。

manifest 摘要位于 `historical_manifest_summary.json`。

## 6. ManifestSubject

历史 manifest 必须先注册为 exactly-one-reference `ManifestSubject`。subject
digest 同时绑定 historical manifest、evidence policy 和 reconstruction basis，
然后才允许创建 unified v2 batch。普通来源和历史来源共享同一 subject adapter，
不新增历史专用 authority。

## 7. 统一 authority

历史回填继续使用：

- `UnifiedAuthorityRepository`；
- v8.3-A `create_import_batch` 和 `transition_batch`；
- v8.3-E `ApprovalArtifact` validator；
- v8.3-E `ExecutionBundle` 和 execution journal；
- v8.3-E verification engine；
- v8.3-E writer lease、`recovery_required` 和 rollback engine。

运行时对 approval、journal、lease、verification 的 alias identity 作断言。若任一
alias 漂移，执行在写入前以 `unified_authority_alias_mismatch` 失败。代码中不存在
第二套历史状态机或 inactive historical authority builder 调用。

## 8. HistoricalBackfillPlan

计划模型绑定 manifest subject、unified batch、production TargetSnapshot、v8302
digest、evidence policy、reconstruction basis、source/attachment candidates、
conflicts、unknowns 和精确 verification expectations。plan digest 复用 v8.3-A
canonical `make_plan_digest`，时间与路径不进入摘要。

相同 snapshot 与证据集产生相同 plan digest；snapshot、evidence 或 reconstruction
basis 任一变化都会产生新计划并要求新 approval。公开计划只包含摘要 digest 和
聚合数量，见 `backfill_plan_summary.json`。

## 9. Source binding

planner operation whitelist 为：

`verify_native_source_binding`、`create_legacy_source_binding`、
`create_historical_attachment_binding`、`verify_media_identity`、
`record_legacy_unknown`、`record_backfill_conflict`、
`flag_probable_binding`、`skip_existing_binding`。

native 只验证；deterministic legacy 必须 exact-plan approval 后写历史 binding；
probable 默认 rejected/deferred；unknown 不写 binding；conflict 只写统一 conflict
ledger 并保持 blocked。旧 source/source_id 永不更新。

## 10. Attachment binding

attachment binding 只保存 logical media ID、source attachment digest、message
关系、occurrence index、状态、size conflict、evidence、provenance 和 unified
batch。`resolved` 之外 `entity_digest` 必须为 `NULL`；本节点不创建
`AssetEntity`、文件或资产晋升。

## 11. Media compatibility

E4-1A `ym1_` public media identity 和 `ysa1_` synthetic source identity 规则被
直接复用。descriptor 相同不等于实体相同；size conflict 不自动选择某个 size。
回填前后 media occurrence/message 关系、media ID、attachment JSON 和
`entity_available=0` 均保持不变。聚合状态见 `compatibility_summary.json`。

## 12. Clone migration

证据工具按固定顺序：

1. 只读核验生产门禁；
2. SQLite online backup 创建一致性可抛弃克隆；
3. 应用并验证 v8301 marker/digest；
4. 应用并验证 v8302 marker/digest/inventory；
5. 执行 integrity/FK；
6. 验证旧逻辑摘要不变和 v8302 表为空；
7. 从 production TargetSnapshot 生成 manifest、subject 和 plan。

所有 writable clone 均位于系统临时目录；ignored output 只保存脱敏 JSON。

## 13. Clone execution

exact-plan approval 通过后，统一执行入口在单事务中追加 manifest、subject、batch、
events、approval、execution、lease、source/attachment bindings 和 verification。
执行前记录旧表 row counts、ID set、critical fields、source、attachment JSON、
FTS、desktop query、message-context 和 conversation-messages digest；执行后逐项
复核，不只比较计数。

真实 clone 结果见 `clone_execution_summary.json`。

## 14. Compatibility invariants

必须同时满足 conversations/messages/assets counts、conversation/message ID sets、
source 值、attachment JSON、created/updated critical fields、FTS、desktop query、
message-context、conversation-messages、media identity、integrity 和 FK 全部不变。
任一 invariant 失败都不能进入 completed。

`compatibility_summary.json` 保存每项布尔结果与前后 contract digest。

## 15. Approval

统一 `ApprovalArtifact` 精确绑定 plan digest、TargetSnapshot、ManifestSubject、
全部 operation IDs、conflict decisions 和有效期。reconstruction basis 与 evidence
policy 通过 plan digest 被不可分割地绑定。不允许 wildcard、跨 snapshot 重用、
probable 自动批准或 conflict 默认通过。

`production_historical_backfill_approval_package.json` 固定
`production_execution_authorized=false`，它只是未来生产评审输入，不构成授权。

## 16. Idempotency

首次 clone 执行写入精确 delta；completed 计划重放复用既有 execution 并返回
`exact_plan_replay_skipped`，不重复插入 source/attachment binding。binding identity
或 target 冲突 fail-closed，绝不自动覆盖。snapshot、evidence、policy 或
reconstruction basis 改变均要求新 plan/approval。

结果见 `idempotency_summary.json`。

## 17. Failure/recovery

故障演练覆盖 pre-commit、attachment interruption、journal failure、旧表写入、
verification failure、commit 后 completed 前中断、post-commit、rollback failure
和成功 restore。pre-commit 事务完整回滚；post-commit 无法确定时进入统一
`recovery_required`；不无限重试、不删除未知 ingestion 记录、不在旧表保存恢复状态。

事件链 tamper、lease conflict、schema drift、invalid model/digest 和 approval expiry
由单元测试覆盖。结果见 `failure_recovery_summary.json`。

## 18. Production approval package

审批包包含 subject、reconstruction/evidence、production fingerprint、
v8301/v8302、plan、operation/binding/deferred/unknown/conflict counts、affected
tables、零旧表更新、compatibility、idempotency、failure/recovery、clone result、
operator checklist 和 expiry。包中明确保持生产执行未授权。

## 19. 隐私与安全

生产路径和 `data/`/`imports/` writable target 被硬拒绝；clone 必须名为
`target.sqlite` 且位于系统临时目录或本节点 ignored output。SQL 私有值全部参数化。
日志和 JSON 不保存正文、标题、完整业务 ID、绝对路径、URL、Tailnet、token、
cookie 或原始 ZIP；实现无网络调用，也不读取 stash 内容。

## 20. 测试结果

本节点新增 5 个 test modules 和 1 个 fixture module，共 50 个新测试。它们覆盖
Evidence、ManifestSubject/plan、统一 authority、clone execution、compatibility、
idempotency、failure/recovery、media 与 privacy/security。

完整回归只运行一次：1,319 tests，全部通过，5 skipped，unittest reported
160.798 seconds（wall 161.532 seconds）。机器可读结果位于
`full_regression_summary.json` 和 `acceptance_summary.json`。

## 21. 已知限制

- 原始历史 archive 不可用；manifest 必须保持 `archive_state=unavailable`。
- 当前 legacy null-source 记录只有在受控导入报告证据链成立时才可归为
  deterministic；证据撤销时必须回落 unknown。
- 当前生产 `entity_available=0`，本节点不推断实体内容或路径。
- 当前实数审计没有 probable、unknown 或 conflict 并不消除相关 fail-closed 分支；
  它们由模型、执行和恢复测试持续覆盖。
- 本节点没有生产执行授权。

## 22. v8.3-H 进入条件

只有在 v8.3-G 代码审阅、完整回归、四库/imports/stash 门禁、clone compatibility、
idempotency、failure/recovery 和非授权审批包全部通过，并由用户另行明确授权后，
才可规划 v8.3-H。当前工作区必须保持 G 改动未暂存、未提交，且不得自动进入 H。
