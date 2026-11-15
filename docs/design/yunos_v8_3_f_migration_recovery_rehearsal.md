# YunOS v8.3-F — Schema Migration and Recovery Rehearsal

## 1. 节点边界

v8.3-F 只建立迁移方案、一次性克隆演练、故障注入、恢复验证和未来人工审批包。生产数据库始终是只读来源；所有 DDL、synthetic import、rollback 和 restore 仅发生在系统临时目录或 `output/v8.3-f-migration-recovery-rehearsal/` 内的可抛弃数据库。生产迁移入口固定抛出 `production_migration_not_authorized`，本节点没有生产 writer、快捷执行命令或自动 approval。

## 2. 当前 schema

只读审计得到受管 schema 指纹 `0000000000000000000000000000000000000000000000000000000000000000`。影子 FTS 表不进入受管指纹，顶层对象为 8 个表、11 个索引、1 个 trigger：

- 旧表：`assets`、`conversation_marks`、`conversation_tags`、`conversations`、`memory_summaries`、`message_marks`、`messages`、`messages_fts`。
- 旧 trigger：`messages_ai`，负责 messages 到 FTS5 的 INSERT 同步。
- 旧数据：426 个 conversations、16,840 条 messages、172 个 assets。
- PRAGMA：`user_version=0`、`application_id=0`、`page_size=4096`；只读 immutable 审计视图报告 `journal_mode=delete`。
- `conversations` 和 `messages` 有 `source_platform`，没有 `source_id`；attachment 信息仍位于旧 `metadata_json`。
- `messages.message_id` 不是 PK/unique，因此新表不得用外键强行约束到它。
- 旧表没有可安全补上的 ingestion foreign key；不允许为此重建或改写旧表。

不能修改或删除 `conversations`、`messages`、`assets`、FTS 虚拟表、旧索引、`messages_ai`，也不能改变旧 ID、source 值、metadata/attachment JSON。v8.1 桌面查询、v8.2 search、message-context、conversation-messages 的字段和排序契约是硬兼容门禁。

## 3. v8.3 additive schema

迁移新增 13 个 ingestion 表：

1. `ingestion_source_manifests`
2. `ingestion_batches`
3. `ingestion_batch_events`
4. `ingestion_source_bindings`
5. `ingestion_asset_entities`
6. `ingestion_attachment_bindings`
7. `ingestion_conflicts`
8. `ingestion_decisions`
9. `ingestion_approvals`
10. `ingestion_executions`
11. `ingestion_execution_events`
12. `ingestion_verifications`
13. `ingestion_leases`

另增 `schema_migrations` 和 `yunos_schema_meta` 两个 marker 表、11 个必要索引、12 个 append-only trigger。新表之间使用 foreign key；旧 message/conversation ID 只作为审计绑定值保存，不向旧表添加外键。来源身份组合、logical media、decision/conflict、approval digest、verification/execution 等有明确 unique 门禁。长期 `storage_key` 只允许相对 key，拒绝盘符、绝对路径、反斜线开头和 `..`。所有 JSON 写入先经过 canonical JSON round-trip，表级再用 `json_valid` 检查。

迁移后、synthetic 执行前，13 个 ingestion 表全部为空；不会自动为旧数据回填 source binding，不会为历史 attachment occurrence 伪造 entity。

## 4. migration marker

推荐并实现 migration version `8301`：

- migration id：`migration_yunos_v8_3_ingestion_core_001`
- migration name：`yunos_v8_3_ingestion_core`
- migration digest：`0000000000000000000000000000000000000000000000000000000000000000`
- marker schema：`v8.3-f-migration-marker-1`
- target schema fingerprint：`0000000000000000000000000000000000000000000000000000000000000000`

`schema_migrations` 同时绑定前后 schema fingerprint、应用版本、rehearsal id 和 applied time；`yunos_schema_meta` 保存 canonical meta value 及其 digest；`PRAGMA user_version=8301` 只是第三重标记，不能单独构成生产审批依据。marker history 禁止 UPDATE/DELETE。

## 5. MigrationPlan

`build_migration_plan()` 从只读 `SchemaSnapshot` 构建固定计划，计划包含 source/target fingerprint、完整 statement digest 列表、对象增量、零破坏性变更和估算增长。`validate_migration_plan()` 重新计算固定迁移摘要并拒绝计划漂移、DDL 摘要漂移、任何顶层 DROP/ALTER/DELETE/UPDATE。预期新增 15 tables、11 indexes、12 triggers，估算初始增长 524,288 bytes。

## 6. 一致性克隆

`create_consistent_backup()` 要求源 WAL 为零，并使用 SQLite online backup API。生产源以 `mode=ro&immutable=1` 打开，避免普通 WAL read-only 连接触碰生产 SHM。克隆前后检查 DB/WAL/SHM gate、schema fingerprint、计数、ID 集合摘要、关键字段摘要和 `integrity_check`。WAL/SHM 不作为克隆的长期依赖。

本次真实生产结构克隆和迁移前 backup 均通过逻辑等价及 integrity 验证。

## 7. 执行协议

克隆迁移顺序为：

1. 读取并绑定 source schema snapshot。
2. 验证 MigrationPlan digest。
3. 验证 free-space 和跨进程独占 lock。
4. `BEGIN IMMEDIATE`。
5. 创建 marker 表。
6. 创建 13 个 ingestion 表。
7. 创建索引和 append-only trigger。
8. 设置 `user_version=8301`。
9. 在事务内计算并验证 target schema fingerprint。
10. 写入两个 marker。
11. COMMIT。
12. 执行 integrity、foreign-key 和 marker/schema 一致性检查。

COMMIT 前异常全部 rollback；COMMIT 后报告异常依靠 marker/schema reconciliation 判断 completed 或 recovery_required。未知状态不自动重试。

## 8. 旧数据兼容

兼容快照只输出摘要，不输出标题、正文、文件名或真实 ID。迁移前后精确比较：

- conversations/messages/assets counts；
- conversation ID 和 message ID 集合摘要；
- 旧关键字段逻辑摘要；
- source/source_id presence 与值摘要；
- attachment metadata JSON 摘要；
- v8.1 桌面列表查询摘要；
- FTS row count 与 join 契约摘要；
- message-context 代表性窗口摘要；
- conversation-messages 代表性分页字段摘要；
- readonly open、integrity 和 foreign-key check。

真实克隆的全部兼容检查通过，426/16,840/172 保持不变，新表初始为空。

## 9. synthetic end-to-end

演练使用既有 v8.3-C ChatGPT Adapter synthetic fixture 构建 SourceManifest 和 ImportPlan，生成 AssetStagePlan、ApprovalArtifact，运行 E Core Preflight、test-only atomic execution、Verification 和 E rollback。随后仅把这些阶段的 digest 绑定到迁移克隆中的 12 个持久审计表；lease 为短期资源，最终保持 0 行。

该流程没有把生产正文复制到 synthetic 对象或报告。旧表兼容摘要在 synthetic 写入前后完全一致。synthetic 后再用 migrated-empty backup 恢复克隆，证明整库恢复可撤销新审计记录。

## 10. 失败注入

证据矩阵覆盖 17 个场景并全部按预期 fail-closed：

- marker 后失败、部分表后失败、index 后失败、trigger 后失败、COMMIT 前失败；
- COMMIT 后报告失败、integrity 报告失败、foreign-key 报告失败；
- migrator crash、外部 schema 漂移；
- 磁盘空间不足、backup 创建失败、backup 损坏；
- migration digest 不匹配、未知同名表、更高未知版本；
- 并发 migrator。

COMMIT 前场景无 marker 残留且 schema fingerprint 回到源值；COMMIT 后可确认一致时 reconciliation 返回 completed，外部 schema 漂移返回 recovery_required。

## 11. backup/restore

恢复只接受 rehearsal boundary 内的 clone 和 backup。backup 必须先通过 immutable `integrity_check` 与逻辑摘要校验；restore 先在线备份到 sibling temporary DB，验证后才替换目标 clone，并精确移除该 clone 的 WAL/SHM/journal sidecar。生产恢复仍要求人工停服和单独授权。

## 12. recovery_required

以下状态进入 recovery_required：

- marker 缺失或不完整；
- marker 记录的 resulting fingerprint 与真实 schema 不一致；
- 表、索引或 trigger 漂移；
- COMMIT 后验证失败且无法证明 schema 完整；
- 未知外部 schema 修改。

返回结果包含明确人工动作 `stop_and_restore_verified_backup`，`automatic_retry=false`；未知状态永不标记 completed。

## 13. 重复迁移

相同 version、相同 digest、marker 与真实 schema 完全一致时返回 `already_applied`。同 version 不同 digest、未知 migration history、更高未知 user_version、缺 marker、marker/schema drift 均阻断。计划器不会删除未知表或覆盖不兼容 schema。

## 14. schema drift

受管 schema fingerprint 排除 SQLite 内部对象和 FTS shadow tables，但包含所有顶层旧表、虚拟表、索引、trigger 以及新对象。marker 的 resulting fingerprint 必须等于当前受管 fingerprint；人工删除任一 ingestion index/trigger、修改新表或增加未知 schema 都可被检出。

## 15. 生产审批包

`build_production_migration_approval_package()` 只生成脱敏 package，不批准执行。包内包含 migration identity/digest、source/expected/current fingerprint、backup 计划、对象增量、零删除/零旧字段变更、增长估算、兼容/故障/rollback 摘要 digest、风险、人工停止点、expiry 和 operator checklist。包不包含正文、私人文件名、绝对路径、远程网络标识或 credential。

## 16. 人工门禁

未来生产迁移必须同时满足：

1. v8.3-F 正式提交关闭；
2. 固定 MigrationPlan digest；
3. 全部 clone、故障和恢复演练通过；
4. v8.1/v8.2/API/桌面兼容通过；
5. synthetic E2E 通过；
6. 重新确认 DB/WAL/SHM 与 imports tree；
7. 受控停止移动/远程访问服务；
8. 用户明确批准；
9. 迁移后人工验收；
10. 任一异常立即停止并恢复 verified backup。

当前 `apply_production_migration()` 始终拒绝。

## 17. 测试结果

F 专项测试位于 5 个文件，共 34 项，定向运行 34/34 通过。真实克隆证据显示：

- migration completed；
- compatibility passed；
- synthetic passed；
- 17/17 failure injection passed；
- E rollback 与 clone restore passed；
- 演练运行期间 production 内容 gate、imports tree 和 stash OID 未变化。

完整回归按节点约束只在最终验收运行一次：1,217/1,217 通过，5 个均为既有环境型 skip，0 failure、0 error；相对 E 节点的 1,183 项恰好新增 34 项 F 测试。

## 18. 已知限制

- 本节点没有生产 writer 或生产 migration authority。
- 旧 `message_id` 不是 unique，故新 attachment/source binding 不用 FK 指向旧 messages。
- `application_id` 维持 0，避免改变旧应用识别契约。
- online backup 仅在 WAL 为零时从 immutable 源执行；非零 WAL 必须先进入人工受控停写窗口。
- 本次首次普通 `mode=ro` backup 探针和只读完整回归虽未改变 DB/WAL/SHM 内容，却改变了生产 SHM mtime。主控最终裁定 mtime 为只读访问产生的环境观测值，不构成内容修改；SHM SHA-256 和 32,768-byte 大小保持不变，因此 F 内部验收改判 GO。
- 禁止修改或回写 SHM 时间戳。后续环境观测基线记录为 `1785250868045225800`；未来仍以 DB/WAL/SHM SHA-256、大小和 WAL 状态作为主要内容门禁。

## 19. v8.3-G 进入条件

进入 v8.3-G 前仍必须满足：

- F 正式提交；
- F 的 1,217 项回归、克隆迁移、兼容、故障注入、恢复和 synthetic E2E 维持通过；
- DB/WAL/SHM 内容门禁、imports tree 和 stash OID 维持不变；
- 用户另行明确批准下一节点。

本节点正式关闭结论为 GO；未执行生产迁移、生产 ImportPlan、真实资产晋升、push、merge 或 tag。
