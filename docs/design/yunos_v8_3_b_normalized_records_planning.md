# YunOS v8.3-B — Normalized Records and Planning Engine

状态：内部实现验收候选
基线提交：`54e1273d238f2e2a7050c27d02b1339d2de3c87a`
实现分支：`codex/v8.3-b-normalized-records-planning`

## 1. 节点定位与边界

v8.3-B 在 v8.3-A 的 `SourceManifest`、`ImportBatch`、canonical digest 和事件/租约基础上，完成规范化记录模型与确定性 planning engine。

新增：

- `memory/ingestion_planning.py`
- `tests/test_v83_normalized_records.py`
- `tests/test_v83_planning_engine.py`
- 本文档

本节点不执行：

- ChatGPT full/incremental Adapter；
- DeepSeek/Claude Adapter；
- ZIP 解压或文件枚举；
- SQLite schema migration；
- 生产数据库写入；
- 媒体复制、暂存或晋升；
- approval receipt；
- backup、atomic commit 或 rollback；
- 移动端、Web、Tailscale 修改。

Planner 只接受已经规范化的内存对象、脱敏的生产快照描述和 v8.3-A 指纹结构。它没有数据库、文件系统、网络或 Adapter 权限。

## 2. 九类统一模型

`NORMALIZED_MODEL_TYPES` 锁定九类：

1. `SourceManifest`（复用 v8.3-A）
2. `ImportBatch`（复用 v8.3-A）
3. `NormalizedConversation`
4. `NormalizedMessage`
5. `NormalizedAttachment`
6. `AssetEntity`
7. `ImportConflict`
8. `ImportDecision`
9. `ImportVerification`

七个新增模型均为 frozen dataclass。嵌套 metadata/status 使用递归不可变表示，防止构造后改变 hash 输入。

规范化记录属于受控私有 staging：为未来 Adapter 保真，它可以在内存中保留来源原始 ID、标题、消息正文和原始文件名。公共计划只能消费 `plan_descriptor()`；descriptor 不包含上述私人值。

## 3. 来源身份、provenance 与 locator

`RecordProvenance` 记录：

- `source_type`
- `manifest_id`
- 来源原始 record ID（私有字段，`repr=False`）
- `batch_id`
- `adapter_version`
- `parsed_at`
- `record_kind`
- `RecordLocator`

`source_identity` 使用 `record_kind + source_type + source stable ID` 的 canonical SHA-256：

- 不依赖绝对路径；
- 不依赖当前 manifest，因此同一来源稳定 ID 可跨导出匹配；
- manifest lineage 仍由 provenance 单独保存；
- 不同来源平台的相同原始 ID 不会碰撞。

`RecordLocator` 只保存：

- locator 类型；
- ordinal；
- 私有 locator material 的 SHA-256；
- locator protocol version。

原始 member path、JSON 路径或本机路径不进入 plan、conflict、decision 或 report。

## 4. Canonical normalization 与记录哈希

规范化协议：`v8.3-b-normalized-records-1`。

继承 v8.3-A canonical JSON 规则：

- UTF-8；
- Unicode NFC；
- object key 排序；
- 固定 null 语义；
- deterministic finite number；
- 拒绝 NaN/Infinity；
- 不使用 Python 随机 `hash()`；
- 深度和节点数门禁。

额外限制：

- 标题、正文、metadata 有明确大小上限；
- NUL、非法 surrogate 和非 UTF-8 表示 fail-closed；
- optional 来源字段为 null 时必须有 `missing_reasons`；
- warning/error 只保存 reason code；
- raw metadata 先 canonical round-trip，再冻结。

每个记录区分：

- `record_id`：来源稳定身份 + normalization protocol；
- `content_hash`：规范化内容；
- `structure_hash`：父子/会话/消息结构；
- `field_hashes`：标题、时间、正文等单字段辅助证据。

因此“正文相同但时间不同”可以保留独立记录，同时通过 text field hash 产生相似证据。

## 5. Conversation、Message 与 Attachment

### NormalizedConversation

包含来源 provenance、私有标题、原始时间、来源状态、message identity 顺序摘要、content hash、warning/conflict state、可空 production ID 和 missing reasons。

标题/时间/状态进入 content hash；message identity 顺序进入 structure hash。消息顺序改变不会伪装成内容无变化。

### NormalizedMessage

包含来源 conversation/message/node ID、role、author、content type、私有正文、status、weight、时间、parent/children、受控 metadata、content/structure hash、warning/conflict state、可空 production ID。

正文改变会改变 content hash；父子关系改变会改变 structure hash。Planner 只看到正文 field hash，不看到正文。

### NormalizedAttachment

包含来源 attachment/conversation/message ID、私有原始文件名、安全显示名、声明/检测 MIME、声明/实际大小、内容 SHA-256、archive locator、entity state、unavailable reason、asset entity ID、metadata/content hash、warning/conflict state和可空 production binding。

`entity_available` 必须同时具备 hash、实际大小、检测 MIME 且没有 unavailable reason。其他状态必须明确 reason；缺少的 MIME/大小/hash 必须有 missing reason。

## 6. Asset、Conflict、Decision 与 Verification

### AssetEntity

身份由内容 SHA-256、实际大小和检测 MIME 构造；绝对路径不参与身份。只允许固定 root ID + 安全相对路径。绝对、drive、`..` 和路径逃逸被拒绝。

### ImportConflict

确定性字段：

- batch、record kind、source identity；
- production identity digest；
- conflict type；
- evidence hashes；
- risk level；
- allowed decisions；
- status/schema version。

不保存正文、文件名、原始路径或明文 production ID。

### ImportDecision

绑定 conflict 或 plan action scope、decision、actor kind、policy version 和 evidence hash。`decided_at` 不进入 decision identity，使自动决策可重放；policy/evidence 改变会产生不同 ID。

### ImportVerification

模型覆盖 precommit/postcommit/restore 三阶段，绑定 DB/asset snapshot、expected/actual counts、integrity/FK/index/source-binding/completed-state checks 和结果。本节点只实现模型，不运行生产验证。

## 7. 生产快照抽象

Planner 通过 `ExistingRecord` 接收只读、脱敏的生产记录摘要：

- record/source kind；
- opaque source identity；
- production identity digest；
- content/structure/field hashes；
- parent identity 与时间；
- attachment metadata digest；
- asset content SHA-256 和大小。

`production_snapshot_digest` 同时绑定：

- v8.3-A `DatabaseFingerprint`；
- 排序后的全部 `ExistingRecord.snapshot_descriptor()`。

重复 existing binding 会触发 `ambiguous_existing_binding`，不会按遇到顺序选一个。

## 8. Planning Engine

`plan_import()` 的固定阶段：

1. 验证 manifest、batch、adapter、normalized record provenance 完整绑定；
2. 限制最大记录数；
3. 建立现有记录唯一索引；
4. 检测当前快照内重复 source identity；
5. 验证 conversation/message/attachment 关系；
6. 比较来源稳定 ID、content/structure/field hashes；
7. 生成新增、append、no-change、metadata-only、share-entity、link-candidate、conflict 或 tombstone-candidate action；
8. 为安全自动 action 生成版本化 `ImportDecision`；
9. 汇总 action/conflict/warning/decision 计数；
10. 绑定生产快照、normalized records、asset inventory、policy 和 commit prerequisites；
11. 生成不可变 `plan_id`。

输入次序和 `created_at` 不影响 plan ID。policy、adapter、normalization、manifest、DB snapshot、normalized records、asset inventory、action、conflict、自动 decision 或计数变化都会改变 plan ID。

## 9. 去重与冲突矩阵

| 场景 | v8.3-B 行为 |
| --- | --- |
| 同 source ID、content 和 structure 相同 | `no_change` |
| 同 message ID、正文/内容变化 | `message_content_changed`，高风险 |
| 同 ID、结构变化 | `relationship_changed` |
| 同 conversation ID、标题/时间变化 | `conversation_fields_changed` |
| 新消息晚于已有消息 | `append` |
| 旧时间 + 新 ID | `old_time_new_id`，禁止静默跳过 |
| 同正文、不同消息 ID | `append`，附 `similar_content_different_identity`，保留两个来源 |
| 当前快照内重复 ID | `duplicate_source_identity` |
| 同 message ID 跨 conversation | `duplicate_source_id_cross_parent` |
| conversation/message parent 缺失 | 显式 relationship conflict |
| 完整来源快照缺少已绑定记录 | `tombstone_candidate`，从不自动 delete |
| 非完整快照缺少记录 | 不推断删除 |
| 疑似同一逻辑 conversation | 同时 `insert` 和 `link_candidate`，不自动 merge |
| attachment 关系缺失 | `attachment_relationship_missing` |
| metadata 相同、实体 hash 不同 | `asset_metadata_content_mismatch` |
| 实体 SHA/大小相同 | `share_entity`，来源 attachment 关系不合并 |
| SHA 相同、大小不同 | `hash_collision_or_corruption`，critical |
| metadata-only | 明确 action/reason，要求审批 |

任何 conflict action 都没有自动 conflict decision。Planner 不产生 `overwrite` 或 `delete` action。

## 10. ImportPlan 与审批绑定

`ImportPlan` 保存：

- plan/batch/manifest/archive identity；
- adapter、normalization、policy version；
- production snapshot digest；
- normalized records digest；
- asset inventory digest；
- 排序后的 actions、conflicts 和自动 decisions；
- counts；
- approval-required；
- source-snapshot-complete；
- commit prerequisites；
- created_at/schema version。

固定 prerequisites：

- `approval_digest_matches_plan`
- `asset_stage_revalidated`
- `database_snapshot_revalidated`
- `single_writer_lease_active`

计划只描述未来提交意图。v8.3-B 不批准、不备份、不持有 writer lease、不打开生产 DB，也不晋升资产。

## 11. 隐私、安全与资源边界

公共 plan/conflict/decision/report 禁止出现：

- 来源原始 ID；
- conversation title；
- message text/author；
- 原始 filename；
- 原始 locator/path；
- 明文 production ID；
- credential、Tailnet 信息或异常堆栈。

已实现：

- 私有字段 `repr=False`；
- plan descriptor 单向 hash；
- production identity digest；
- reason-code-only warning/error；
- 记录数、text、title、metadata 上限；
- 非有限数字和非法 Unicode拒绝；
- 深层 metadata 继承 canonical JSON 门禁；
- Asset 相对路径门禁；
- ambiguous existing binding fail-closed；
- batch/manifest/provenance/adapter/protocol 漂移 fail-closed；
- SHA 相同大小不同 critical conflict；
- 无数据库、文件、ZIP 或网络 I/O。

## 12. 测试结果

最终验收结果：

- v8.3-B 新增测试：54/54 通过；
  - `tests/test_v83_normalized_records.py`：23 项；
  - `tests/test_v83_planning_engine.py`：31 项；
- v8.3-A Manifest/Batch/Ledger 定向回归：73/73 通过；
- 既有 ChatGPT/DeepSeek/E4-1A 定向回归：52/52 通过；
- 完整仓库回归：94 个测试模块、993 项测试通过，4 项按既有条件跳过，0 失败；
- 仓库既有 privacy scan：9 条历史文档告警；
- 本阶段 4 个新增文件 privacy scan：0 条新增命中。

覆盖九类模型、canonical identity、missing reasons、不可变 metadata、privacy descriptor、conversation/message/attachment/asset、conflict/decision/verification、plan 稳定性、完整冲突矩阵、无静默覆盖和资源门禁。

生产数据库家族验收：

| 文件 | 大小 | 验收前后 SHA-256 | 结果 |
| --- | ---: | --- | --- |
| 主 SQLite | 60,878,848 | `0000000000000000000000000000000000000000000000000000000000000000` | 大小、内容、mtime 均未变化 |
| 主 SQLite WAL | 0 | `0000000000000000000000000000000000000000000000000000000000000000` | 保持空文件；大小、内容、mtime 均未变化 |
| 主 SQLite SHM | 32,768 | `0000000000000000000000000000000000000000000000000000000000000000` | 大小与内容未变化；既有只读回归连接只更新了 mtime |

DB/WAL/SHM 内容门禁通过；v8.3-B 核心模块本身没有数据库、文件、ZIP 或网络 I/O。

## 13. 后续 Adapter/生产迁移

v8.3-C 才能开始 ChatGPT Adapter。进入前必须：

1. Adapter 只产生本模块的 normalized records，不打开生产写连接；
2. full/incremental 都完整清点来源包，由同一 planner 决策；
3. Adapter 不生成 approval 或覆盖 conflict；
4. source locator 使用受控 locator fingerprint；
5. 私有 normalized records 只进入批次 staging，不进入日志/UI；
6. attachment inventory 与消息关系完整；
7. 现有初始/增量脚本保持兼容，不能在 v8.3-B 被替换。

生产 schema migration、source binding 表、approval、backup、commit、asset promotion 和 recovery 均保留给后续明确授权节点。

## 14. 停止点与内部结论

完成验收后保持：

- v8.3-B 改动未暂存、未提交；
- 暂存区为空；
- 不进入 v8.3-C；
- 不修改现有 Adapter/导入器；
- 不迁移或写生产数据库；
- 不复制媒体；
- 不 push、不 merge、不创建 tag。

只有新增测试、完整回归、隐私检查和 DB/WAL/SHM 内容门禁全部通过后，v8.3-B 才可给出内部 GO。内部 GO 不等于授权生产导入或进入 v8.3-C。
