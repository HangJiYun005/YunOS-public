# YunOS v8.3-D — Asset Preservation and Staging Core

## 1. 节点边界

本节点建立：

`NormalizedAttachment → AssetOccurrence / AssetEntityDescriptor → AssetStagePlan → Controlled Batch Staging → AssetStageResult`

它只处理经过 v8.3-B Planner 授权的、已在来源归档中验证过的实体候选。模块不打开生产 SQLite，不迁移 schema，不调用旧 ChatGPT/DeepSeek writer，不执行 ImportPlan，不下载外部 URL，不写入 `imports/chatgpt`，不恢复生产缺失媒体，也不把 verified 文件晋升到生产。

所有测试写入只允许位于系统临时目录或：

`output/v8.3-d-asset-preservation-staging/`

## 2. occurrence 与 entity

`NormalizedAttachment` 继续是 v8.3-B 的规范化逻辑附件。v8.3-D 新增通用 `AssetOccurrence`，只保存一条消息与一个逻辑附件之间的关系：

- occurrence ID；
- normalized attachment ID；
- source type；
- 来源 attachment/message identity 的 SHA-256；
- entity state / entity digest；
- 声明与实际 size/MIME；
- locator kind/fingerprint；
- 警告码。

原始文件名、消息 ID、会话 ID、绝对路径不进入 StagePlan 身份材料。

`AssetEntity` 继续复用 v8.3-B 模型。v8.3-D 用 `AssetEntityDescriptor` 聚合可暂存实体：

- 只有已验证 SHA-256 才能成为 `entity_digest`；
- 同 SHA-256 在单批次只形成一个物理操作；
- 多个 attachment/occurrence 可绑定同一 operation；
- descriptor 相似、文件名相同或 locator 相同均不能证明字节实体相同；
- 同 SHA-256 的 size/MIME 证据冲突时 fail-closed。

`metadata_only`、`entity_missing`、`entity_blocked` 和 `entity_invalid` 不生成 StageOperation。它们仍保留 occurrence 元数据和 Planner 的 metadata-only 决策。

## 3. StagePlan

`AssetStagePlan` 绑定：

- `ImportPlan.plan_id`；
- ImportPlan identity payload 的 canonical digest；
- `manifest_id`；
- `batch_id`；
- `asset_inventory_digest`；
- 去重后的 `AssetStageOperation`；
- 完整 occurrence bindings；
- `expected_asset_delta`；
- `AssetStageFingerprint`。

Planner 中只有 `record_kind=asset` 且 `action_type=insert` 的实体可以转为 StageOperation。额外实体、计划外运行时来源、缺少 normalized entity 的资产 action 均被拒绝。

`expected_asset_delta` 包含：

- `entity_count`；
- `occurrence_count`；
- `physical_bytes`；
- `logical_bytes`。

构造时会将这些值与 operations、bindings 和 fingerprint 逐项重算，不一致即拒绝。

## 4. 确定性摘要

复用 v8.3-A 的 `make_plan_digest`、`normalized_json_digest` 和 `build_asset_stage_fingerprint`。

- operation ID 由 batch、manifest、ImportPlan digest、entity SHA-256、实际大小、检测 MIME 和 normalized entity ID 确定；
- StagePlan digest 绑定 operations、依赖、occurrence bindings、expected delta 和 stage fingerprint；
- 输入集合在构造时排序；
- 输入顺序与 occurrence 顺序不影响摘要；
- 运行时绝对路径、ZIP 文件路径和 member name 不进入计划；
- locator path/fingerprint 变化不改变逻辑 StagePlan digest；
- manifest、batch、ImportPlan 或 entity digest 变化会改变计划；
- dependency 必须引用计划内 operation，且必须无环。

## 5. 受控暂存根

批次目录固定为：

```text
<allowed-test-root>/
  batches/
    <batch-id>/
      incoming/
      verified/
      quarantine/
      ledger/
```

`validate_stage_root` 在创建目录前检查：

- 目标必须位于系统 temp 或本节点 output 根；
- batch ID 必须符合 `batch_<32 lower hex>`；
- 拒绝 `..`、反斜杠 traversal、URL 编码和双编码 traversal；
- 拒绝 UNC、`\\?\`、`\\.\`、ADS 和额外盘符注入；
- 拒绝 trailing dot/space；
- 根自身、父级和批次子目录不得是 symlink/junction/reparse point；
- canonical path 必须仍位于允许根；
- production `data` 与 `imports` 不可能成为允许根。

每个批次建立绑定 batch ID 与 StagePlan digest 的 `ownership.json`。回滚只信任该所有权记录和计划派生文件名。

## 6. 流式写入

`ZipAssetSource` 是运行时绑定对象，持有 operation ID、ZIP 路径和 member name；这些私人定位信息不被序列化或写入 ledger。

每个 operation：

1. 校验取消、超时、磁盘余量和批次预算；
2. 只读打开本地普通 ZIP；
3. 验证 entry 数、唯一 member、非 link、压缩比和声明大小；
4. 通过 `ZipExtFile.read(chunk_size)` 分块读取；
5. 同时累计实际大小、SHA-256 和 MIME sniff 前缀；
6. 以随机、无来源名的 `.part` 写入 `incoming`；
7. flush + fsync；
8. 校验实际 size、SHA-256、MIME；
9. 同一批次卷内 `os.replace` 原子移动到 `verified`；
10. 再次从 verified 文件重算三项证据；
11. 写入 append-only JSONL ledger。

大文件不会整体读入内存。当前实现固定串行读取，因此不会超过 `max_concurrent_reads=1` 的实际并发；配置仍显式保留未来受控并发接口。

## 7. 文件命名

verified 文件名固定为：

`sha256_<64 lower hex>`

incoming/quarantine 文件名只包含 operation ID、安全 reason code 和随机 nonce。物理文件名不含：

- 原始显示文件名；
- attachment ID；
- conversation/message ID；
- ZIP member path；
- 绝对路径。

如果同 digest 文件名已存在，会重新校验 size、SHA-256 和 MIME；同内容复用，不同内容 fail-closed。

## 8. MIME、大小和 hash 验证

执行阶段以实际流字节为权威：

- 实际大小必须同时匹配 ZIP metadata 与 StageOperation；
- 实际 SHA-256 必须匹配 `entity_digest`；
- MIME 由安全的内容签名/UTF-8 文本 sniff 得出并匹配 operation；
- PNG、JPEG、GIF、PDF、ZIP、UTF-8 text 和 generic octet-stream 目前有明确规则；
- 声明 MIME/大小与 verified entity 不一致在计划阶段形成 warning；
- 实际 MIME/大小/hash 不一致导致失败，临时文件进入 quarantine；
- 验证失败文件绝不进入 verified。

## 9. 容量门禁

`AssetStageLimits` 同时约束计划与执行：

- 单实体最大字节；
- 单批物理字节；
- 单批逻辑字节；
- 最大实体数；
- 最大 occurrence 数；
- 最大 ZIP 压缩比；
- 最大 ZIP entry 数；
- 最大并发读取数；
- 单 operation 最长时间；
- 最小剩余磁盘空间；
- chunk size。

计划阶段先验证声明预算；执行阶段按实际字节再次验证。超限停止新增写入，当前 incoming 文件进入 quarantine，不重试且不产生伪 verified 文件。

## 10. ledger

ledger 是批次目录内的 prototype JSONL，不是生产 ingestion ledger 或 SQLite。

每个事件只包含：

- sequence；
- operation ID；
- entity digest；
- previous/next state；
- bytes processed；
- reason code；
- previous event digest；
- event digest；
- ledger version。

事件使用 canonical JSON 和 SHA-256 形成链；每次 append 后 flush + fsync。`state.json` 通过同目录临时文件和 `os.replace` 更新。ledger 不记录正文、原始文件名、URL 或绝对路径。

状态覆盖：

- discovered；
- planned；
- staging；
- verifying；
- staged；
- staged_with_warnings；
- failed；
- cancelled；
- recovery_required；
- rolling_back；
- rolled_back。

## 11. 并发租约

批次使用 `ledger/lease.json` 的 `O_CREAT | O_EXCL` 获取单 writer 租约。租约绑定 batch、StagePlan 和 token digest：

- 同一批次第二 writer 被拒绝；
- inspection 不接管现有租约；
- 恢复必须提交原始 lease token；
- 不按时间自动判定或接管“过期”租约；
- 正常结束验证 token 后释放租约。

## 12. 取消与恢复

取消在打开来源前和每次分块读取前检查。取消时：

- 当前 incoming 文件进入 quarantine；
- 状态转为 `cancelled`；
- 不留下 verified 冒充项。

崩溃恢复遵循：

- incoming 残留永不算可用；
- verified 文件必须重算 size/MIME/SHA-256；
- ledger 与文件树不一致为 `recovery_required`；
- verified 已完成但 ledger 尚未完成时，只有准确 lease authority 可补记 verifying/staged；
- 不自动删除或接管未知文件；
- 未完成的已知 incoming 可在准确 lease authority 下隔离；
- 错误 lease token 拒绝。

## 13. 安全回滚

回滚先验证：

- allowed root；
- batch ID；
- ownership record；
- StagePlan digest；
- 独占 lease；
- 当前 ledger state。

它只删除当前 StagePlan 派生的 `verified/sha256_<digest>` 和匹配当前 operation ID 的 incoming 临时文件。未知文件保留并产生 `unknown_file_preserved`。一个批次的回滚不会访问另一个批次目录；两个批次即使具有相同实体摘要也保持隔离。

## 14. Planner 集成

v8.3-D 不修改 `ImportPlan`。`build_asset_stage_plan` 只读取其已冻结字段：

- asset insert → 一个候选实体 operation；
- share_entity / no_change → 不复制新实体；
- metadata_only / missing / blocked → 不产生实体 operation；
- conflict / reject → 不进入 staging；
- asset inventory digest 与 ImportPlan 保持绑定；
- stage fingerprint 可供未来 approval/commit 再绑定。

运行时来源映射必须与 operation ID 集合完全相等，防止计划外实体进入。

## 15. 生产兼容

生产兼容审计是只读的：

- 当前媒体 occurrence 应保持 1,138；
- 当前逻辑媒体身份应保持 1,133；
- `entity_available` 应保持 0；
- 当前生产 metadata-only/missing 状态应生成 0 个真实 StageOperation；
- 既有 Claude `ym1_` / `ysa1_` identity 不改变；
- v8.3-C ChatGPT identity 不改变；
- DeepSeek 记录不被本模块识别为可暂存实体。

本节点不把生产缺失实体伪装为空文件，也不创建正式媒体目录。

## 16. 测试结果

专项测试文件：

- `tests/test_v83_asset_stage_plan.py`
- `tests/test_v83_asset_staging.py`
- `tests/test_v83_asset_staging_security.py`
- `tests/test_v83_asset_staging_recovery.py`

覆盖计划确定性、去重与 bindings、流式写入、空/大文件、MIME/size/SHA-256、路径与 ZIP 安全、容量、取消、超时、磁盘不足、租约竞争、崩溃窗口、ledger 篡改、恢复和安全回滚。

验收结果：

- v8.3-D 专项：82/82，失败 0、跳过 0；
- v8.3-A：73/73；
- v8.3-B：54/54；
- v8.3-C：51/51，保留 1 个既有 Windows symlink 条件跳过；
- 媒体身份/E4-1A：60/60；
- 旧 ChatGPT/DeepSeek：6/6；
- 其余 v8.2 核心：75/75；
- 完整回归只运行一次：1,140/1,140，失败 0、既有条件跳过 5。

生产门禁复核：

- DB SHA-256、size、mtime 不变；
- WAL 仍为 0 bytes，SHA-256、mtime 不变；
- SHM SHA-256 和 32,768 bytes 大小不变；完整回归的真实只读入口更新了 SHM mtime；
- `imports/chatgpt` 保持 14 个文件、55,212,991 bytes，树 SHA-256 不变；
- `stash@{0}` OID 不变；
- 暂存区为空。

## 17. 已知限制

- 本节点只实现本地 ZIP runtime source；目录 source 可在未来通过同一 operation ID resolver 接口加入。
- MIME sniff 是封闭白名单，不替代完整媒体解析器；未知二进制统一为 `application/octet-stream`。
- 物理去重在单批次内完成；跨批次仍是隔离副本，避免引入全局共享存储和跨批次删除风险。
- crash recovery 需要调用方持有精确 lease token；不提供自动过期接管。
- ledger 是节点临时目录中的 prototype JSONL，不进入生产 DB。
- 没有 production promotion、正式恢复、审批 UI、HTTP 媒体服务或移动端行为。

## 18. v8.3-E 进入条件

进入后续节点前必须另行批准并定义：

1. production asset root 和 alias/reparse-safe authority；
2. 全局内容寻址存储及跨批次引用计数；
3. StagePlan/ImportPlan/approval digest 的最终事务绑定；
4. 生产 writer、备份、恢复点和数据库事务顺序；
5. promotion 后的完整性检查与回滚协议；
6. MIME policy 的正式白名单和媒体解析器；
7. 租约持久化、人工恢复 authority 和运维流程；
8. 缺失媒体恢复来源的合法性、真实性和审批边界。

这些条件未满足前，v8.3-D 的 verified 文件只能是隔离暂存证据，不能视为生产可用实体。
