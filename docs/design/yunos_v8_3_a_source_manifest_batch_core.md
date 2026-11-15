# YunOS v8.3-A — Source Manifest and Import Batch Core

状态：内部实现验收候选
节点边界：仅建立统一导入 Core 与原型元数据账本，不连接生产导入器
基线提交：`521cdc543ae7aca19143878e61da445d926dba90`
实现分支：`codex/v8.3-a-source-manifest-batch-core`

## 1. Core 模块边界

v8.3-A 采用“纯业务 Core + 独立原型存储”两层结构：

- `memory/ingestion_core.py`：数据模型、规范化摘要、只读源扫描、状态机、事件重放、重复来源判定和 repository 协议。该模块不知道生产数据库路径，也不执行 SQL。
- `memory/ingestion_ledger.py`：实现测试/原型 SQLite repository、不可变事件表和跨进程 writer 租约。路径门禁只允许系统临时目录或仓库相对目录 `output/`。
- `tests/test_v83_ingestion_manifest.py`：Manifest、规范化 JSON、归档元数据与路径安全。
- `tests/test_v83_ingestion_batch.py`：Batch、摘要、状态机、事件重放与重复来源决策。
- `tests/test_v83_ingestion_ledger.py`：repository、SQLite 不可变性、隐私、租约和双进程竞争。

本节点没有修改以下既有入口：

- `tools/chatgpt_export_importer.py`
- `tools/chatgpt_incremental_import.py`
- `tools/deepseek_importer.py`
- 生产数据库/schema
- E4-1A media 实体索引
- 移动端、Tailscale、审批 UI

静态审计和 `immutable=1` 只读探针得到的旧边界如下：

- 生产主库（仓库 `data/` 下的既有主库）`PRAGMA user_version=0`。
- `conversations` 具有 `source_file`、`source_platform`；`messages` 具有 `source_platform`；`assets` 没有统一 source 字段。
- 当前没有 `source_manifests`、`import_batches`、`batch_events` 表。
- 旧增量导入器以 `BEGIN IMMEDIATE` 包围写入和校验，但没有统一 manifest/batch/event 语义。
- 旧备份采用带时间戳的独立文件名；该逻辑没有被 v8.3-A 调用。
- E4-1A `mediaId` 继续使用独立 `ym1_` 命名域；v8.3-A 的 `manifest_`、`batch_`、`event_` 身份不进入或复用该命名空间。

因此 v8.3-A 的生产迁移数量为零。

## 2. SourceManifest schema

`SourceManifest` 是 frozen dataclass，字段固定为：

| 字段 | 语义 |
| --- | --- |
| `manifest_id` | `manifest_` + 规范化身份载荷 SHA-256 |
| `source_type` | 脱敏、受限 ASCII token |
| `source_version` | 来源格式版本 |
| `adapter_name` | 仅记录适配器身份，不包含实现对象 |
| `adapter_version` | 参与 manifest 身份 |
| `archive_sha256` | 固定缓冲区流式 SHA-256 |
| `archive_size` | 扫描时字节数 |
| `archive_kind` | 归档类别 token |
| `discovered_at` | 规范 UTC 时间；不进入身份 |
| `scanned_at` | 规范 UTC 时间；不进入身份 |
| `record_counts` | 有界、非负、聚合记录计数 |
| `asset_counts` | 有界、非负、聚合资产计数 |
| `warnings` | 只允许 reason code；原始错误被替换为 `redacted_error_detail` |
| `validation_status` | 只读扫描结果状态 |
| `source_locator_kind` | 仅保留脱敏定位器类型 |
| `source_locator_fingerprint` | 由定位器协议、类型和内容 SHA-256 构造 |
| `manifest_schema_version` | manifest schema 版本，参与身份 |

身份载荷不包含时间、绝对路径或文件名。同一字节内容、同一来源/适配器/schema 在不同路径重复扫描得到同一 `manifest_id`；内容、适配器版本或 schema 版本变化会得到不同 ID。

扫描门禁：

- 以固定大小块流式读取，不整体载入大文件。
- 每块前支持取消；结束后再次检查取消。
- 扫描前、文件句柄内和扫描后比较 size、mtime、device、inode/file-id；前后 `lstat` 额外比较 change time。
- Windows 使用只共享读取的文件句柄，扫描期间拒绝并发写入或替换。
- symlink、junction/reparse point、UNC、设备路径、路径穿越和非普通文件 fail-closed。
- 只持久化 SHA-256 和聚合元数据，不保存 ZIP 字节、成员正文、原始路径或文件名。

## 3. ImportBatch schema

`ImportBatch` 是 frozen dataclass，字段固定为：

| 字段 | 语义 |
| --- | --- |
| `batch_id` | 随机 UUID4 的 128 bit 十六进制，前缀 `batch_` |
| `manifest_id` | 不可变来源绑定 |
| `source_type` | manifest 来源类型 |
| `adapter_name` / `adapter_version` | 适配器绑定 |
| `batch_mode` | `full`、`incremental`、`historical_backfill`、`verification_only` |
| `created_at` / `updated_at` | 规范 UTC 时间 |
| `current_state` | 状态机当前快照 |
| `plan_digest` | 排除非语义字段后的计划摘要 |
| `approval_digest` | manifest、plan、DB-before、asset-stage 和审批策略的联合摘要 |
| `database_before_fingerprint` | 提交前数据库指纹，可空 |
| `database_after_fingerprint` | 验证后数据库指纹，可空 |
| `asset_stage_fingerprint` | 资产阶段指纹，可空 |
| `warning_count` / `error_count` / `conflict_count` | 有界非负计数 |
| `event_sequence` | 与账本快照一致的最后序号 |
| `recovery_status` | `not_required`、`required`、`rolling_back` 或 `rolled_back` |
| `batch_schema_version` | `v8.3-a-import-batch-1` |

`batch_id` 用于区分同一 manifest 的不同尝试，不承担幂等语义。重复来源由 manifest 和已有批次状态共同判断。

## 4. 状态机

锁定状态集合：

`discovered`、`validating`、`validation_failed`、`planned`、`awaiting_approval`、`approved`、`committing`、`verifying`、`completed`、`completed_with_warnings`、`failed`、`recovery_required`、`rolling_back`、`rolled_back`、`cancelled`。

允许转换：

| 前态 | 允许后态 |
| --- | --- |
| `discovered` | `validating`, `cancelled` |
| `validating` | `validation_failed`, `planned`, `cancelled` |
| `validation_failed` | `validating`, `failed`, `cancelled` |
| `planned` | `awaiting_approval`, `cancelled` |
| `awaiting_approval` | `approved`, `planned`, `cancelled` |
| `approved` | `committing`, `planned`, `cancelled` |
| `committing` | `verifying`, `failed`, `recovery_required` |
| `verifying` | `completed`, `completed_with_warnings`, `failed`, `recovery_required` |
| `failed` | `validating`, `rolling_back`, `cancelled` |
| `recovery_required` | `rolling_back` |
| `rolling_back` | `rolled_back`, `recovery_required`, `failed` |
| 终态 | 无后续转换 |

额外门禁：

- `approved -> committing` 必须重新计算并精确匹配 approval digest。
- 提交调用方必须提供当前 `manifest_id`，阻止来源变化后复用旧审批。
- `completed_with_warnings` 必须已有至少一个 warning。
- 非首事件必须绑定前一事件 digest。
- 所有未列出的转换统一返回 `illegal_state_transition`。

这明确阻止 `discovered -> completed`、`planned -> committing`、`failed -> completed`、`recovery_required -> completed`、`completed -> committing` 和 `cancelled -> committing`。

## 5. 事件账本

`BatchEvent` 是 frozen dataclass，每次状态转换产生一条事件。必需字段包括：

- `batch_id`
- `sequence`
- `event_type`
- `previous_state`
- `next_state`
- `occurred_at`
- `actor_kind`
- `reason_code`
- `payload_digest`
- `event_schema_version`

篡改检测扩展字段：

- `previous_event_digest`
- `event_digest`
- `event_id`

`event_digest` 覆盖所有事件语义字段和前一事件 digest；`event_id` 为 `event_` + `event_digest`。payload 本身不写入账本，只写入其规范化 digest。原始错误、消息正文、路径、ZIP 内容和网络标识因此不会出现在事件记录。

原型 SQLite 使用 `(batch_id, sequence)` 唯一约束，并对 `batch_events` 建立拒绝 UPDATE/DELETE 的触发器。事件插入与 batch 快照更新在同一 `BEGIN IMMEDIATE` 事务内完成。

重放从固定初态 `discovered` 开始，逐条验证：

1. batch 绑定；
2. sequence 从 1 连续增长；
3. previous digest 链；
4. 前态等于已重建状态；
5. 转换存在于白名单；
6. event identity/digest 可重算；
7. 重建状态和 sequence 与 batch 快照一致。

任一序列缺口、链断裂、字段篡改或快照漂移均 fail-closed。

## 6. 指纹和摘要

统一规范化协议为 `v8.3-a-canonical-json-nfc-1`：

- object key 排序；
- UTF-8；
- Unicode NFC；
- 无额外空白；
- 禁止 NaN 和正负 Infinity；
- finite float 使用 Python JSON 的最短 round-trip 表示；
- `-0.0` 被有意保留，与 `0.0` 摘要不同；
- 禁止进程随机 `hash()`；
- 非字符串 key、重复字段和 NFC 后冲突字段 fail-closed；
- 深度、节点数和输入字节数均有上限。

摘要构造：

- archive：固定缓冲区流式 SHA-256。
- normalized JSON：规范字节的 SHA-256。
- plan：递归排除时间戳、绝对路径、路径、文件名、mtime 等固定非语义字段后计算。
- approval：绑定 approval policy、manifest ID、plan digest、DB-before fingerprint 和 asset-stage fingerprint。
- database：保存内容 SHA-256、size、schema descriptor digest、logical descriptor digest，不保存数据库路径。
- asset stage：输入仅为 logical-key digest、content SHA-256 和 size；排序后生成 content digest 与 layout digest，不保存真实资产名或路径。

## 7. 重复来源判定

`decide_source_replay()` 始终返回明确枚举和稳定 reason code：

| 条件 | 决策 |
| --- | --- |
| 没有相同 archive | `rescan_allowed` |
| 相同 archive 但 adapter/schema 产生不同 manifest | `incompatible_manifest` |
| 相同 manifest 有 `completed` / `completed_with_warnings` | `exact_duplicate` |
| 相同 manifest 有 committing/verifying/recovery/rollback 中间态 | `recovery_required` |
| 相同 manifest 上次 failed/validation_failed | `retry_allowed` |
| 相同 manifest 已 planned/awaiting/approved | `approval_required` |
| 相同 manifest 正在 discovered/validating | `blocked` |
| 仅有 cancelled/rolled_back 或无批次 | `rescan_allowed` |

判定不使用文件名。相同文件名但内容不同由 archive SHA-256 区分；路径不同但内容相同保持同一 manifest。已审批来源发生内容变化后，新的 manifest 无法匹配旧 approval digest。

## 8. Repository 接口

Core 只声明三个协议：

- `ManifestRepository`
  - `add_manifest`
  - `get_manifest`
  - `find_manifests_by_archive_sha256`
- `BatchRepository`
  - `add_batch`
  - `get_batch`
  - `list_batches_for_manifest`
- `EventRepository`
  - `append_transition`
  - `list_events`

`SQLiteIngestionLedger` 是 v8.3-A 唯一实现，同时满足三个协议。业务状态机中没有 SQL；未来迁移生产 schema 时可以增加新 repository，而不改变 manifest identity、状态白名单或事件重放规则。

原型表：

- `ingestion_ledger_schema`
- `source_manifests`
- `import_batches`
- `batch_events`
- `writer_leases`

所有测试数据库位于系统临时目录；没有 ledger 文件写入 `data/` 或加入版本控制。

## 9. 并发和单 writer

writer 租约同时占用两个 SQLite 唯一 scope：

- `manifest:<manifest_id>`
- `target:<target_id>`

因此：

- 同一 manifest 同时只有一个合法 writer；
- 同一目标同时只有一个合法 writer；
- 不同 manifest 且不同目标可以并行；
- 租约记录 owner、created_at、expires_at、batch_id、manifest_id、target_id 和随机 lease group ID；
- acquire 使用独立连接与 `BEGIN IMMEDIATE`，不依赖进程内全局变量；
- 两个真实 Python 进程竞争时只有一个得到 `acquired`，另一个得到 `blocked`。

过期租约不会被 acquire 自动删除或替换。遇到过期记录返回 `recovery_required`，保留旧租约作为恢复证据。处于 `committing` 的 batch 丢失租约后，由 `mark_lost_committing_lease()` 追加事件并进入 `recovery_required`。

释放租约必须同时匹配 batch、lease group 和 owner；不匹配时不删除任何记录。

## 10. 隐私边界

长期对象禁止保存：

- 绝对路径；
- 原始文件名或 ZIP 成员名；
- 会话标题或消息正文；
- 完整来源/附件 ID；
- 原始 ZIP 内容；
- Cookie、Authorization、token 或密码；
- Tailnet 域名/IP；
- 原始异常堆栈。

允许保存：

- 脱敏 source/adapter/schema token；
- 聚合数量；
- SHA-256 和规范化 digest；
- 不含路径的结构化指纹；
- reason code；
- 批次状态和不可变事件链。

异常只向持久化层传稳定 reason code。非白名单错误字符串统一转换为 `redacted_error_detail`。事件 payload 只计算摘要，不保存 payload。

## 11. 安全威胁与资源限制

已实现并测试：

- 大源包固定缓冲区流式哈希；
- 扫描中取消；
- 扫描期间文件变化；
- Windows 并发写/替换排他；
- symlink、junction/reparse point；
- UNC 和 Windows 设备路径；
- `..` 路径穿越；
- ZIP bomb 元数据（条目数、单成员大小、总展开大小、压缩比、路径深度）；
- 归档成员绝对路径、drive path、link 和 traversal；
- manifest 计数与 warning 数量上限；
- JSON 字节、深度和节点数上限；
- 非法 UTF-8、未配对 surrogate；
- 重复字段和 NFC key 冲突；
- NaN/Infinity；
- 恶意错误信息与路径泄露；
- 非法状态转换；
- 事件缺口、链篡改和快照篡改；
- batch ID 冲突；
- 双进程 writer 竞争；
- 过期/丢失租约恢复。

本节点没有导入 `zipfile` 执行真实解压，没有网络调用，也没有复制媒体实体。

## 12. 测试结果

新增测试文件共 73 项，当前结果：

- Manifest / canonical JSON / archive metadata：25 项；
- Batch / digest / state / replay decision：31 项；
- SQLite repository / privacy / concurrency：17 项；
- 合计：73/73 通过。

关键验收证据：

- 状态机测试遍历所有声明的合法转换和所有未声明转换；
- 双进程测试结果精确为一个 `acquired`、一个 `blocked`；
- SQLite 直接 UPDATE/DELETE 事件均被触发器拒绝；
- 人为移除触发器后修改事件 JSON，读取时仍由 digest 校验发现；
- ledger 文件字节扫描不包含注入的私有路径、消息正文、Authorization 文本或 Tailnet 测试值；
- 92 个顶层 Python 测试模块共 939 项，最终退出码为 0；其中 4 项沿用既有条件跳过，无失败或错误；
- ChatGPT 增量导入定向回归 4/4、DeepSeek 导入定向回归 2/2、E4-1A media index/recovery 定向回归 46/46；
- 现有隐私扫描器报告 9 条历史文档基线告警；v8.3-A 新增文档命中数为 0。

生产 DB/WAL/SHM 门禁：

| 文件角色 | 测试前 SHA-256 / size / mtime UTC | 测试后 SHA-256 / size / mtime UTC | 结论 |
| --- | --- | --- | --- |
| DB | `0000000000000000000000000000000000000000000000000000000000000000` / 60,878,848 / `2026-07-12T10:06:06.9995384Z` | 相同 / 相同 / 相同 | 内容和大小不变 |
| WAL | `0000000000000000000000000000000000000000000000000000000000000000` / 0 / `2026-07-12T10:06:07.1647636Z` | 相同 / 0 / 相同 | 仍为空文件 |
| SHM | `0000000000000000000000000000000000000000000000000000000000000000` / 32,768 / `2026-07-25T15:38:52.8375433Z` | 相同 / 32,768 / `2026-07-25T17:28:04.2339606Z` | 内容和大小不变；既有真实库只读回归触发 mtime 元数据漂移 |

门禁按本节点锁定规则通过：DB 内容/大小不变，WAL 保持 0 字节，SHM 内容/大小不变；没有生产 schema 迁移或生产导入写入。

## 13. 未来生产迁移方案

v8.3-A 不批准生产迁移。未来节点若要进入生产，至少需要：

1. 单独设计生产 schema migration，不能复用原型路径门禁或直接复制测试 DB。
2. 对生产目标建立 backup/checksum 前置门禁。
3. 用新的 production repository 实现三个既有协议。
4. 迁移前后验证旧 conversations/messages/assets、FTS、marks、summaries 和 E4-1A mediaId 不变。
5. 明确生产 lease 的进程崩溃、主机重启和人工恢复流程。
6. 将现有 ChatGPT 初始/增量导入器通过后续适配层接入，而不是在 v8.3-A 改造它们。
7. 为每个 adapter 单独做 source schema、隐私和回放验收。
8. 审批 UI 只能消费已封存的 plan/approval digest，不得绕过状态机。

生产迁移必须作为独立提交和独立授权节点，且需要再次执行 DB/WAL/SHM 门禁。

## 14. v8.3-B 进入条件

v8.3-B 目前未开始。进入前必须同时满足：

- v8.3-A 新增测试全部通过；
- 完整仓库回归通过；
- ChatGPT 初始/增量、DeepSeek、E4-1A 定向回归通过；
- 生产 DB 的内容 SHA-256 和 size 不变，WAL 保持 0 字节，SHM 的内容 SHA-256 和 size 不变；三者 mtime 均需前后记录并解释任何元数据漂移；
- 隐私扫描确认新增文件无私有正文、真实文件名、绝对路径、凭据或 Tailnet 信息；
- `git diff --cached --name-only` 为空；
- v8.3-A 改动保持未暂存、未提交；
- 没有生产 schema 迁移、生产导入、媒体复制、push、merge 或 tag；
- 内部验收结论明确为 GO。

即使以上条件满足，也只代表允许提出 v8.3-B 计划，不代表自动授权开始 v8.3-B。
