# YunOS v8.3-C — Unified ChatGPT Adapter

## 1. 节点边界

本节点只建立 `Source Archive → ChatGPT Adapter → SourceManifest → Normalized Records → Asset Inventory → v8.3-B Planner → ImportPlan`。Adapter 是只读来源解析器，不含生产 SQL，不执行 ImportPlan，不建立 writer，不迁移 schema，不复制媒体实体，不访问网络。

实现位置：

- `memory/ingestion_adapters/chatgpt.py`
- `memory/ingestion_adapters/__init__.py`
- `memory/media_index.py` 中新增的公开媒体来源身份兼容入口

本节点没有修改生产会话、消息、附件或媒体文件。

## 2. 旧 full / incremental 差异审计

| 审计项 | 旧初始导入器 `tools/chatgpt_export_importer.py` | 旧增量导入器 `tools/chatgpt_incremental_import.py` | v8.3-C 结论 |
|---|---|---|---|
| 来源形态 | 仅 ZIP | 仅 ZIP，并调用初始导入器的 ZIP loader | 统一支持 ZIP、解压目录、单 JSON 测试源 |
| `conversations.json` | 支持主文件和数字分片，以 basename 匹配 | 完全复用初始 loader | 保留分片兼容；新增重复 basename、大小写冲突和缺失门禁 |
| ZIP 安全 | 直接 `ZipFile.read()`，没有 central-directory 限额、ZIP Slip、symlink、压缩比或扫描变化门禁 | 同左 | 废止；central directory 先验、受控流式读取、变化检测和配置化限额 |
| 会话图 | 遍历整个 `mapping`，跳过空 node，再按 `create_time` 排序 | 完全复用 | 废止按时间扁平化；显式处理 `current_node`、parent/children、分支、orphan、cycle、空 node |
| 主路径 | 不使用 `current_node`，所有非空 node 都会进入输出 | 同左 | `active_path_compat` 只规划 active path；排除分支必须进入统计和 warning |
| 审计路径 | 无 | 无 | `all_reachable_audit` 可清点所有可见消息，但不得生成额外写操作 |
| 消息 ID | `message.id or node_id` | 同左，并用 message ID / `(conversation_id,node_id)` 去重 | 保留优先级；增加 node/message 不一致证据、确定性 synthetic fallback 和冲突 fail-closed |
| 时间 | 秒级本地时区 ISO | 微秒级 UTC ISO | 统一为微秒级 UTC `Z`；非法/缺失时间进入 warning 和 missing reason |
| 正文 | 字符串 part 拼接；字典 part 尝试 `text/content`；附件名称和未知 part 被写入占位正文 | 完全复用 | 废止附件元数据拼正文；NFC、统一换行、保留代码；未知 part 进入 typed structured part 和 warning |
| structured content | 被压成正文或占位字符串 | 同左 | 保留 typed `structured_parts`，通过 v8.3-B 私有 `normalized_metadata` 承载 |
| 附件 | 只读取 `conversation_asset_file_names.json`，记录名称和 ZIP 声明大小；不关联消息 occurrence | 增量路径不导入附件 | 完整清点消息 occurrence、逻辑身份、locator 指纹、实体状态、大小/MIME/hash |
| 资产字节 | 不验证内容 hash / MIME | 不处理 | ZIP/目录实体只流式读；计算 SHA-256、实际大小、MIME；不落正式文件 |
| 来源字段 | 初始独立库不写 `source_platform` | 写 `source_platform=chatgpt`、`memory_space=general` | 规范化 provenance 固定 `source_type=chatgpt`；不写生产库 |
| 去重 | 无跨目标语义 | 会话 ID、消息 ID 和 node fallback；相同 ID 直接跳过 | 全部交给 v8.3-B Planner；同 ID 同 digest 为 no-change，不同 digest 为 conflict |
| 旧时间新 ID | 初始全量可进入独立库 | `create_time <= max_create_time` 被静默跳过并计数 | cutoff 行为废止；任何稳定新 ID 都进入 Planner |
| 缺时间新会话 | 可进入独立库 | 直接终止增量计划 | 进入规范化 warning；是否冲突由 Planner 决定 |
| 报告 | 含标题、正文、私人文件名的 SQLite/CSV/JSONL/Markdown | 打印含绝对 ZIP/DB 路径的 dry-run | v8.3-C 输出只含 hash、opaque identity、计数、reason code 和脱敏摘要 |
| 写入边界 | 重建独立 SQLite 和报告 | 内置生产备份、事务、insert、commit | Adapter 中全部禁止；旧 `execute()` 路径不被调用 |

需要兼容的行为是官方 conversation ID、官方 message ID、node fallback、角色/作者、时间、status/weight、数字分片和现有 `source_platform=chatgpt` 语义。必须废止的行为是时间 cutoff、按时间扁平化整个 mapping、静默跳过旧时间新 ID、附件名称拼正文、未知 part 丢失、绝对来源路径进入结果，以及解析器内生产写入。

## 3. Adapter 接口

`ChatGPTAdapter` 提供：

- `probe_source()`
- `scan_manifest()`
- `validate_source()`
- `enumerate_conversations()`
- `enumerate_messages(branch_policy=...)`
- `enumerate_assets()`
- `normalize_conversation()`
- `normalize_message()`
- `normalize_attachment()`
- `build_normalized_bundle(batch_mode=...)`
- `produce_import_plan(target_snapshot=..., batch_mode=...)`
- `verify_scan_determinism()`

同时提供 `probe_source()`、`scan_chatgpt_source()`、`normalize_chatgpt_source()` 和 `plan_chatgpt_import()` 函数式入口。所有入口只解析、规范化或规划，不执行 action。

## 4. 来源验证

`AdapterLimits` 配置 entry 数、单 entry 大小、总解压声明大小、压缩比、路径深度、JSON 大小/深度/node 数、正文长度和流式 chunk。

扫描先拒绝：

- 不存在、非普通文件/目录；
- symlink、junction/reparse point；
- `../`、反斜杠变体、绝对路径、盘符、UNC、`\\?\`、`\\.\`；
- ZIP symlink、重复 entry、大小写冲突；
- 过多 entry、过大 member、总大小越界、压缩比越界；
- 缺失或重复 `conversations*.json`；
- JSON 重复键、非法 Unicode、非法数字、过深/过大结构；
- 扫描期间 size/mtime/device/inode 证据变化；
- 调用方取消。

ZIP 不整体解压。JSON 受最大字节数限制；资产字节经 `ZipExtFile` 或普通文件流式读取。

## 5. 会话图策略

每个会话先构建 node、parent 和推导 children 关系，再校验声明 children。图审计记录：

- mapping node；
- active path message；
- branch message；
- empty node；
- missing-parent/orphan node；
- cycle node 和 parent/children 非法关系；
- hidden/deleted message；
- message ID / node ID 不一致；
- 重复 message identity。

`current_node` 有效时沿 parent 回溯得到 active path。缺失时使用“消息时间证据 + 稳定 source ID”的确定性叶节点 fallback，不使用字典插入顺序。

## 6. `branch_policy`

`active_path_compat` 是唯一可规划默认值。它保留当前主路径消息，允许跳过空 node，并把隐藏/删除消息排除原因写入 warning。

`all_reachable_audit` 返回所有可审计可见消息。该结果不进入 `build_normalized_bundle()` 的生产可规划记录，因此不会凭空增加 ImportPlan action。

所有被 active policy 排除的分支都会进入 `branch_messages` 和 `branch_messages_excluded`，禁止静默消失。

## 7. 消息规范化

消息输出复用 v8.3-B `NormalizedMessage`。字段映射如下：

- source conversation/message/node/parent/children → v8.3-B 稳定来源和结构字段；
- role / author / content type / status / weight → 已验证 token/值；
- created/updated → UTC 微秒 `Z` 或 missing reason；
- text → NFC，CRLF/CR 统一为 LF，保留代码和用户可见文本；
- structured parts / attachment refs / identity kind / normalization version → 私有 `normalized_metadata`；
- warning → `warning_codes`；
- message digest → v8.3-B `content_hash` 和 `structure_hash`。

附件 locator、绝对内部路径和附件元数据不拼入正文。错误只返回 reason code，不打印正文。

## 8. 稳定身份

会话优先使用官方 `conversation_id` / `id`。缺失时使用 `chatgpt-synthetic-conversation-v1` 内容身份。

消息严格按：

1. 官方 `message.id`；
2. mapping node ID；
3. `chatgpt-synthetic-message-v1` 确定性 identity。

synthetic message payload 只使用稳定会话 ID、父子证据、规范化消息结构摘要，不使用路径、运行时间或随机 UUID。message ID 和 node ID 不一致时，官方 message ID 胜出，node ID 仍保留在私有字段并生成 warning。synthetic collision 直接 fail-closed。

## 9. 附件 occurrence 与逻辑身份

Adapter 保留 `AttachmentOccurrence` sidecar，至少包含 source type、source attachment/message identity、identity kind、occurrence index、kind、安全显示名、声明 MIME/大小、locator kind/fingerprint、entity digest/state、warning 和兼容 `ym1_` mediaId。

同一 source attachment identity 的多个 occurrence 不复制成多个 v8.3-B 逻辑 attachment。Planner 接收一个逻辑 `NormalizedAttachment`，完整 occurrence 计数仍保留在 bundle 和 Manifest。相同来源 ID 指向不同 descriptor/entity 时 fail-closed。

## 10. 资产清点

来源中存在实体字节时：

- 流式 SHA-256；
- 实际大小；
- 签名/MIME 检测；
- 声明大小和 MIME 对比；
- path-free archive-member fingerprint；
- v8.3-B `AssetEntity` 仅作为本次扫描的 verified candidate。

没有实体字节时明确区分 `metadata_only`、`missing`、`external_url_blocked`、`size_mismatch` 和 `mime_mismatch`。外部 URL 只做不可逆指纹，不发请求、不跟随重定向、不持久化完整 URL。

本节点没有创建 `.blob`、正式 import 文件或可打开媒体。

## 11. full / incremental 统一语义

两种模式调用相同 Adapter 实例和相同 bundle builder。`normalized_digest`、conversation/message/attachment digest、warning 顺序和 occurrence 顺序一致。

差异只在：

- `ImportBatch.batch_mode`；
- 调用方提供的 `TargetSnapshot`；
- Planner 据此产生的 action/conflict。

没有时间 cutoff。旧时间新 ID 会产生正常 insert/append action。完全来源快照的缺失绑定只产生需要审批的 `tombstone_candidate`，不存在 delete action。

## 12. SourceManifest 集成

直接调用 v8.3-A `build_source_manifest()`。`record_counts` 包含规范要求的 conversation、mapping、active/branch、empty/orphan、attachment、asset、metadata-only、unsupported、warning 和 error 计数；`asset_counts` 包含 native/synthetic、verified、metadata-only、missing、external-blocked、size/mime mismatch。

Manifest 不含标题、正文、私人文件名或绝对路径。

ZIP 的 `archive_sha256` 保留实际容器字节摘要；目录使用 path-relative 内容清单摘要；单 JSON 使用文件字节摘要。容器重排可能改变 v8.3-A manifest / v8.3-B plan identity，这是 manifest 绑定的安全性质。Adapter 另外给出不含容器布局的 `normalized_digest` 和 `semantic_plan_digest`，用于验证 ZIP/目录和 entry 顺序变化时语义 action 一致。

## 13. Planner 集成

`produce_import_plan()` 只调用 `memory.ingestion_planning.plan_import()`。它验证：

- batch/manifest/record provenance 绑定；
- action 只引用合法 normalized record；
- Planner digest 绑定 manifest、target database fingerprint 和 ExistingRecord snapshot；
- full/incremental mode 绑定到确定性 batch ID；
- source 或 target 变化改变安全 plan identity；
- 同 source、target、mode 重复运行得到相同 plan ID、batch plan digest、action 顺序和 warning 顺序。

返回 `ChatGPTPlanningResult`，包含 SourceManifest、bundle、TargetSnapshot、ImportPlan、warnings、audit summary 和 semantic plan digest。没有 execute 方法。

## 14. 生产兼容

生产库只通过 `mode=ro&immutable=1` 和 `PRAGMA query_only=ON` 审计。当前内容证据：

- 总 426 个会话、16,840 条消息；
- 排除 17 个 Claude 会话和 516 条 Claude 消息后，受保护基线为 409 / 16,324；
- ChatGPT 绑定候选消息 10,407；
- legacy unknown 为 177 个会话、5,229 条消息；
- DeepSeek 为 1 个会话、688 条消息；
- conversation/message ID 缺失和重复组均为 0；
- 媒体 1,138 个 occurrence、1,133 个逻辑身份、可用实体 0。

Adapter 只匹配 `source_type=chatgpt`；DeepSeek、Claude 和 `legacy_unknown` ExistingRecord 不会生成 ChatGPT tombstone。由于本节点没有把历史生产库伪装成 v8.3-B normalized digest，也没有把现有 import pack 宣称为权威原始导出，因此没有生成真实生产 ImportPlan。

## 15. 安全限制

- 无网络模块调用；
- 无生产 SQL；
- 无 schema migration；
- 无 writer / backup / transaction；
- 无 stash 操作；
- 无绝对路径、URL、Cookie、Authorization 或正文进入证据；
- 无正式媒体文件；
- 无 action 执行；
- 所有异常只暴露稳定 reason code。

## 16. 测试结果

新增三组测试：

- `tests/test_v83_chatgpt_adapter.py`
- `tests/test_v83_chatgpt_adapter_security.py`
- `tests/test_v83_chatgpt_adapter_planning.py`

另有 `tests/v83_chatgpt_adapter_fixtures.py` 动态生成 JSON、目录和 ZIP；仓库未提交私人或二进制 ZIP 夹具。

当前新增测试共 51 个。来源/图/规范化、身份、附件、资产、full/incremental、跨进程确定性、ZIP entry 顺序、安全、隐私和 Planner 绑定均已覆盖。Windows 环境无法创建 symlink 时，symlink 测试明确 skip；ZIP symlink 和 reparse-point 检查仍有独立实现与测试。

最终验收：

- v8.3-C 新增测试：51/51 通过，1 项按 Windows symlink 条件跳过；
- v8.3-A Manifest/Batch/Ledger：73/73；
- v8.3-B normalized records/planning：54/54；
- media identity / E4-1A：60/60；
- 旧 ChatGPT/DeepSeek：6/6；
- 上述既有定向集合合计：193/193；
- 完整仓库回归只运行一次：1,058 项通过，5 项按既有环境条件跳过，0 失败；
- privacy scan：仍为 9 条历史文档基线告警，本节点新增文件 0 条。

生产文件最终门禁：

- DB SHA-256、65,982,464-byte 大小和 mtime 不变；
- WAL SHA-256、0-byte 大小和 mtime 不变；
- SHM SHA-256 和 32,768-byte 大小不变；
- SHM mtime 由既有基线变化为 `2026-07-28T10:23:39.7712253Z`。变化发生在完整回归启动既有 real-readonly 服务期间，没有字节或大小变化，按本节点规则作为只读元数据触碰单独记录。

## 17. 已知限制

1. v8.3-B 模型已锁定，没有顶层 `structured_parts`、`attachment_refs`、`identity_kind` 和 occurrence 字段；Adapter 将前三者放入私有 `normalized_metadata`，将 occurrence 保留为 typed sidecar，Planner 仍只消费锁定模型。
2. 真实生产库没有 v8.3-C normalized digest，且本节点没有权威原始 ChatGPT 导出，所以只验证只读可表示性和来源隔离，不伪造真实生产 plan。
3. MIME 检测是受控签名前缀加扩展名 fallback，不是反病毒扫描。
4. `all_reachable_audit` 未获产品批准，不能生成额外写 action。
5. 本节点只实现 ChatGPT；DeepSeek/Claude Adapter 不在范围内。

## 18. v8.3-D 进入条件

只有在以下条件全部满足并另行批准后才能进入 v8.3-D：

1. v8.3-C 新增测试和规定回归全部通过；
2. DB/WAL/SHM 内容、大小及规定 mtime 门禁通过；
3. `stash@{0}` 原样；
4. SourceManifest、normalized/semantic digest 和 Planner 证据复核通过；
5. 真实生产导入仍为 NO-GO；
6. 产品明确批准 branch policy、真实来源归属、审批 UI、writer、执行与回滚协议。

v8.3-C 本身停在未暂存、未提交、未 push、未 merge、未 tag 状态。
