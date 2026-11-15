# YunOS v8.3-E — Approval, Atomic Commit and Verification Core

## 1. 节点边界

v8.3-E 把 `ImportPlan + AssetStagePlan + AssetStageResult` 连接到可审计的审批、预检、测试执行、验证、恢复和回滚闭环。本节点没有生产 writer：唯一可写目标由 `initialize_test_execution_target()` 在系统临时目录或 `output/v8.3-e-approval-atomic-commit/` 内创建。

禁止项保持不变：不写生产 SQLite，不迁移生产 schema，不修改 `imports/chatgpt`，不晋升真实媒体，不调用旧增量导入器，不执行真实 ImportPlan，不实现 Web 审批 UI、其他 Adapter、移动端/PWA/Tailscale 或媒体 HTTP API。

## 2. ApprovalArtifact

`ApprovalArtifact` 是路径无关、不可变、确定性模型，绑定：

- batch、manifest、plan ID 与 plan digest；
- stage plan ID 与 stage plan digest；
- 执行前 TargetSnapshot fingerprint；
- 每条冲突的最终决定；
- approved/rejected operation 精确分区；
- `exact_plan`、`verification_only` 或 `rollback_only` scope；
- actor、创建时间、过期时间和 schema version。

`approval_digest` 覆盖上述全部逻辑字段。plan、stage、target、冲突决定或 operation 集合任一变化都会导致重新校验失败。没有来源级或未来计划通配审批。

## 3. ConflictDecision

每条 `ConflictDecision` 包含 conflict ID/type、selected action、允许与拒绝 operation、证据摘要、reason code、actor 和 schema version。校验会确认 conflict 确实属于当前 ImportPlan、动作属于该冲突的允许决定，并重新推导 evidence digest。

`exact_plan` 要求所有冲突各有且仅有一条决定；high/critical 冲突拒绝 `automatic` 或 `policy_engine` actor。决定变化会改变 approval digest。删除候选在 preflight 中强制位于 rejected 集合，绝不转换为 `DELETE`。

## 4. Preflight

Preflight 是无写入门禁。它检查：

- ImportBatch 为 `approved` 且非 recovery 状态；
- manifest、batch、ImportPlan 与 AssetStagePlan 的 ID/digest 绑定；
- AssetStageResult 为当前计划的完整、无 quarantine 结果；
- approved staged entity 的 digest、size、MIME 与文件流式复核；
- TargetSnapshot、测试 schema marker 和 test target identity 未变化；
- approval digest、scope、expiry、冲突决定和 operation 分区；
- operation/action 白名单、payload schema、dependency 闭包和 DAG；
- 删除候选默认拒绝；
- execution root、DB/asset 固定布局、reparse/路径边界；
- 备份与资产预算所需磁盘空间；
- target 与 batch writer lease 当前可获取；
- 不存在 recovery-required marker。

任一失败都不会创建 execution journal、数据库事务或目标资产。

## 5. ExecutionBundle

通过 preflight 后生成不可变 `ExecutionBundle`，至少包含 execution ID、batch/manifest、plan/stage/approval digest、target fingerprint、approved/rejected operations、脱敏 database operation descriptor、approved asset descriptor、VerificationExpectation、backup fingerprint、execution-root logical fingerprint、conflict decisions 和 schema version。

execution ID 由逻辑字段确定性推导；绝对路径和私人正文不参与。operation payload 只保留在内存私有字段，bundle 的公开表示仅保存 payload digest。执行期间不能追加或替换 approved operation。

## 6. 测试数据库执行器

测试目标由固定 schema 初始化，带独立 `execution_schema` marker 和随机 test-target ID。主要表包括 conversations、messages、attachments、asset_entities、source_bindings、execution_applied_operations 与 execution_conflict_resolutions。

执行器只接收 `TestExecutionTarget`，其 DB 固定为目标根下的 `target.sqlite`；生产 DB 路径无法通过初始化器进入。当前生产库没有 v8.3-E test schema marker，readonly compatibility audit 返回 `production_execution_schema_missing`，因此 fail-closed。

## 7. Operation handlers

显式 handler 映射支持：

- `create_conversation`
- `create_message`
- `create_attachment_metadata`
- `register_asset_entity`
- `bind_source_record`
- `update_safe_metadata`
- `mark_metadata_only`
- `verify_existing_record`

以下 operation 只写审计事件，不执行领域写入：

- `skip_exact_duplicate`
- `flag_possible_duplicate`
- `flag_conflict`
- `mark_deletion_candidate`

未知 type 在模型构造时 fail-closed。SQL 中只有固定表/列标识符，所有来源值均参数化。单一 `BEGIN IMMEDIATE` 按 dependency 拓扑执行，唯一约束、外键和事务内 orphan 检查负责重复与关系门禁；任一 handler/SQL 失败回滚整笔事务。

## 8. 资产晋升与 DB 协议

跨 SQLite 与文件系统采用可恢复顺序：

1. preflight；
2. 原子获取 target + batch 双租约；
3. 建立 append-only journal；
4. 在租约内创建字节一致的测试 DB 备份并复核；
5. 流式复核 staged entity，写入 batch pending 文件；
6. `os.replace` 晋升至 `final/sha256_<digest>`；
7. journal 记录 digest 与 `created/reused` ownership；
8. `BEGIN IMMEDIATE`、operation handlers、事务内不变量、commit；
9. 提交后精确验证；
10. completed、脱敏报告、释放租约。

同 digest 既存文件只有内容、size 相同才复用；共享实体不获得本批次 ownership。晋升失败时 DB 尚未开始。DB 失败时 SQLite rollback，并只删除 journal 证明由本批次新建且内容仍匹配 digest 的实体。

## 9. Execution Journal

journal 为 JSONL append-only digest chain，sequence 从 1 连续递增，每条事件绑定 previous digest。读取时字段集合、schema、sequence、execution ID、previous digest 和 event digest 任一异常都会 fail-closed。

事件覆盖 preflight、lease、backup、asset promotion、transaction、operation、commit、verification、completed/failed、recovery、rollback 与 database restore。detail 白名单只允许 operation/entity/digest、布尔 ownership、计数、状态和 reason code；不记录正文、来源文件名或绝对路径。

## 10. Verification

`VerificationExpectation` 绑定：

- conversation/message/attachment/entity/source-binding 精确 delta；
- approved/rejected operation；
- conflict resolution count；
- before DB/asset fingerprint；
- expected after logical-row digest；
- expected identity additions；
- expected final asset digest set。

提交后重新以 readonly/immutable 方式捕获目标，验证数据库物理内容 fingerprint、schema、全表逻辑行、身份集合、资产集合、已有 ID 保留、外键、orphan、source binding/entity 唯一性，以及 approved/rejected/no-unapproved operation。计划外 DB 行、metadata/operation log 变化或计划外资产都会失败。

## 11. rollback

已提交执行只能凭绑定当前 after-target 的 `rollback_only` ApprovalArtifact 回滚。回滚器获取或验证双租约，重放 journal 得到 ownership，删除内容仍匹配的本批次独占实体，保留 shared entity，再以备份原子恢复测试 DB 并复核完整 before fingerprint。

未知文件、变更后的 owned 文件、缺失 ownership 或备份异常不会被猜测性删除；回滚停在 `recovery_required` 并保留人工恢复证据。

## 12. recovery_required

资产晋升后、DB commit 前中断：journal 重放确认未 commit，进入 recovery_required，删除精确 owned entity，确认 DB 仍为 before snapshot 后完成 rolled-back recovery。

DB commit 后、completed 前中断：重放 journal，重新运行完整 verification；通过则补写 verification/completed 并释放租约，否则保持 recovery_required。journal 篡改、租约 authority 不匹配、目标不一致或 cleanup/restore 失败都不能伪装成“未发生”。

## 13. 并发与租约

租约不是进程内变量，而是系统临时目录中的 `O_EXCL` lock files：

- target scope 对测试 DB + asset root 的私有规范路径求 digest；
- batch scope 对 batch ID 求 digest；
- 两把锁使用同一随机 secret 的 hash 作为 authority；
- 获取第二把锁失败会释放本次已获取的第一把锁；
- release/recovery 必须提供 exact secret；
- stale/过期租约不自动接管。

因此同 target 或同 batch 只能有一个 writer；不同 target 且不同 batch 可并行。事务前、commit 前、commit 后再次核验租约；commit 期间丢失租约进入 recovery_required。

## 14. 隐私与安全

路径策略拒绝相对路径、遍历、percent-encoding、UNC、device path、ADS、尾随点/空格、symlink/junction/reparse component、生产 data/imports 目录和固定生产 DB。文件复制使用流式 hash/size 与已知 magic MIME 复核，SQLite 值全部参数化。

公开 bundle、journal、ExecutionResult 和 audit report 不输出 payload、正文、来源文件名、绝对路径、Tailnet、Token、Cookie 或原始 ZIP 内容。E 核心没有 HTTP/socket/network import。

## 15. 生产兼容

现有 A 模型直接复用 SourceManifest、ImportBatch 状态、canonical digest 与 reason-code 规则；B 直接复用 ImportPlan、PlanAction、ImportConflict；D 直接复用 AssetStagePlan、AssetStageResult 与 asset fingerprint。

未来生产 writer 至少需要：

- 明确版本化的 execution schema / repository interface；
- 当前生产 TargetSnapshot 到新 schema 的只读适配；
- 生产级备份、WAL checkpoint 与恢复接口；
- 生产 asset root ownership repository；
- 审批/lease/journal 的持久 repository；
- 经单独节点批准的 schema migration。

这些接口在 v8.3-E 均未接入。生产库 readonly audit 可表达“缺少 test execution schema，因此禁止执行”；不会生成可用于生产的 approval artifact。

验收时以 `mode=ro&immutable=1` 复核生产库仍为 426 个 conversations、16,840 条 messages；对应计划从未传入执行器。

## 16. 测试结果

新增五组 approval/execution/verification/recovery/security 测试以及共用 synthetic fixture。定向测试覆盖确定性审批、冲突、expiry、白名单/DAG、数据库 handlers 与事务回滚、资产 success/shared ownership、commit 前后崩溃、journal 篡改、显式 rollback、计划外 DB/asset、双租约、生产 schema 拒绝和脱敏证据。

2026-07-28 节点验收结果：

- v8.3-E 定向：43/43；
- v8.3-A：73/73；
- v8.3-B：54/54；
- v8.3-C：51/51，保留既有 1 个环境型 symlink skip；
- v8.3-D：82/82；
- 媒体身份/E4-1A：60/60；
- 旧 ChatGPT/DeepSeek：6/6；
- 唯一一次全仓 discovery：1,183/1,183，5 个均为既有环境型 skip；
- synthetic evidence：preflight、execution、verification 与生产前后门禁全部通过。

生产 DB 内容/size/mtime、WAL 0 bytes、SHM 内容/size、`imports/chatgpt` 精确树摘要和 `stash@{0}` OID 均通过规定门禁。SHM mtime 可因 readonly regression attach 改变，不属于本节点要求的 SHM 内容/size 门禁。

## 17. 已知限制

- 只有测试等价 schema，没有生产 repository/writer 或 migration。
- 文件系统与 SQLite 不声称单事务原子性，依赖 journal + ownership + backup 恢复协议。
- stale lease 不自动过期或抢占，必须由持有 exact authority 的恢复流程或人工审计处理。
- rollback approval 由调用方在独立审批渠道生成，本节点没有 UI。
- 已知 MIME magic 只覆盖常见格式；未知格式仍依赖 D 阶段已验证的 MIME descriptor 和 digest/size。
- 未恢复真实缺失媒体；当前 production `entity_available=0` 的计划仍不可执行。

## 18. v8.3-F 进入条件

只有在 v8.3-E 定向测试、分层回归、一次全量回归、DB/WAL/SHM/import tree/stash 门禁全部通过，并由用户正式关闭 E 后，才可讨论 v8.3-F。进入 F 仍需单独决定生产 schema、repository、backup/WAL、资产 ownership、审批渠道和发布回滚策略；本节点不会自动推进。
