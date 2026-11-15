# YunOS v8.3-0 — Unified Ingestion and Asset Preservation Baseline

版本名称：**YunOS v8.3 — Unified Ingestion & Asset Preservation**
中文定位：**多来源统一导入与资产保真**
节点性质：仓库级审计与设计基线
权威起点：`8cec62074e3797f17ac49b30a21bf166eabec115`
工作分支：`codex/v8.3-0-unified-ingestion-baseline`

本节点只新增本文档。没有修改生产导入代码、SQLite、移动端、Tailscale、媒体实体或
现有索引，也没有执行数据库迁移、实体恢复、暂存、提交、push、merge 或 tag。

## 审计摘要

v8.3-0 的仓库级结论如下：

1. 当前存在三条实际来源写入路径：ChatGPT 初始导入、ChatGPT 增量导入和
   DeepSeek 单会话导入；它们没有共享的来源清单、规范化记录、批次账本、冲突规划、
   审批或资产提交协议。
2. ChatGPT 初始导入可以枚举部分 ZIP 资产，但只保存
   `asset_file/original_name/size_bytes`，不复制实体字节。
3. ChatGPT 增量导入只追加 conversations/messages，完全跳过资产清点。
4. 当前生产库有 409 个会话、16,324 条消息和 172 条 `assets` 目录元数据；附件元数据
   共 1,112 条，940 条没有资产目录映射，172 条有目录映射但实体缺失，
   `entity_available=0`。
5. 当前没有 `SourceManifest`、`ImportBatch`、`ImportConflict`、`ImportDecision` 或
   `ImportVerification` 的持久模型；原始 ZIP、原始记录定位和内容哈希也没有形成统一
   审计链。
6. 当前增量导入的备份与单事务写入比初始导入更安全，但仍不是跨数据库和文件系统的
   统一原子协议；仓库也没有主数据库的受控 restore 入口。
7. v8.3 需要一次**后续的加法式数据库迁移**，但 v8.3-0 不执行该迁移。现有会话 ID、
   消息 ID、移动端读取和 FTS 必须保持兼容。

最终判定见第 21 节：**v8.3-0 设计基线 GO；任何新生产导入、数据库迁移或媒体恢复仍
NO-GO。**

## 审计方法和证据边界

本次使用以下只读方法：

- Git 基线、历史提交、入口和调用关系审计；
- 导入器、迁移脚本、schema、测试、备份和恢复能力静态检查；
- 生产数据库 `mode=ro&immutable=1` + `PRAGMA query_only=ON` 结构与聚合查询；
- 受控的 `imports/`、`data/`、`backups/`、`exports/`、`logs/` 文件类型清点；
- 唯一保留 ZIP 的中央目录检查，不解压、不读取消息正文；
- 既有 v8.2-E4 媒体审计、恢复门禁和版本报告交叉核验。

生产 DB/WAL/SHM 在只读探针前后的大小、mtime 和 SHA-256 完全一致。本文不记录私人
会话标题、消息正文、原附件 ID、私人 URL、绝对来源路径或认证信息。

审计核心文件：

- `tools/chatgpt_export_importer.py`
- `tools/chatgpt_incremental_import.py`
- `tools/deepseek_importer.py`
- `tools/run_import_example.ps1`
- `run.ps1`
- `tools/create_test_db.py`
- `tools/backup_memory_db.py`
- `tools/check_chatgpt_db.py`
- `tools/migrate_memory_marks.py`
- `tools/migrate_memory_summaries.py`
- `tools/migrate_semantic_memory.py`
- `docs/schema.sql`
- `docs/chatgpt_importer_public.md`
- `docs/source_platforms.md`
- `docs/local_data_policy.md`
- `docs/safe_git_workflow.md`
- `memory/media_index.py`
- `tests/test_chatgpt_incremental_import.py`
- `tests/test_deepseek_importer.py`
- `tests/test_v82_media_index_recovery.py`
- `docs/design/yunos_v8_2_e4_media_contract_audit.md`
- `docs/design/yunos_v8_2_e4_1a_media_index_recovery.md`
- `docs/design/yunos_v8_2_e4_r0_media_entity_source_audit.md`
- `docs/version_reports/yunos_v8_1_release_closure.md`
- `docs/version_reports/yunos_v8_2_release_closure.md`
- 本地忽略的初始 ChatGPT 处理成果包、其解包副本和聚合报告
- `data/<production-main-db>.db` 及其 WAL/SHM，只读

## 1. 版本定位

YunOS 继续定位为：

> 模型无关的个人 AI 记忆与上下文系统。

v8.3 的职责不是增加另一条临时导入脚本，而是改造“数据如何进入 YunOS”：

- 多来源使用同一套来源、批次、规范化、冲突、审批、提交和验收协议；
- 消息、会话、来源信息和媒体实体能够端到端追溯；
- 原始档案、生产数据、私人整理层和派生索引保持清晰边界；
- 同一来源包可安全重放，失败可恢复，任何不确定数据都不会被静默覆盖；
- 生产 Core 仍是单一权威数据源，移动端仍只读，不引入多设备 SQLite 双向同步。

v8.3-0 只冻结审计事实、目标模型、接口边界、实施顺序和验收门禁。它不实现框架。

## 2. 现有导入架构

### 2.1 当前入口清单

| 入口 | 类型 | 当前作用 | 是否直接写生产库 | 主要缺口 |
|---|---|---|---|---|
| `tools/chatgpt_export_importer.py` | 初始导入 | ZIP → 新 SQLite/CSV/JSONL/报告 | 默认写新输出库 | 可覆盖旧输出；无来源指纹、批次、冲突、审批和实体复制 |
| `tools/chatgpt_incremental_import.py` | 增量导入 | ZIP → 现有生产 conversations/messages | 是，`--execute` 时 | 不清点资产；只接受严格较新会话；无持久批次/冲突账本 |
| `tools/deepseek_importer.py` | 单会话导入 | JSON → 生产 conversations/messages | 是 | 自行确保/修改 schema；无备份、审批、来源档案和统一规划 |
| `tools/run_import_example.ps1` | 示例包装 | 调用初始 ChatGPT 导入器 | 间接 | 硬编码示例形态，不是受控运行入口 |
| `run.ps1` | 主运行器 | 测试库、搜索、Web、备份、迁移、审计 | 部分命令可写 | 没有统一 import/plan/approve/verify/rollback 命令 |
| `tools/create_test_db.py` | 测试夹具 | 只读参考生产 schema，生成合成测试库 | 否 | 不是来源导入器，但当前承担 schema 兼容复制 |
| 三个 `migrate_*` 脚本 | 历史迁移 | marks、summaries、semantic 测试结构 | 视参数而定 | 没有统一版本表、迁移账本和主库恢复协议 |

Markdown、TXT、Claude、Gemini 等当前只有目录或 `source_platform` 枚举，没有可用生产
适配器。DeepSeek 已有历史导入器，但 v8.3 首轮只迁移 ChatGPT；DeepSeek 适配器必须在
后续节点按统一契约重做，而不是直接复用现有写库路径。

### 2.2 当前 ChatGPT 初始导入流程

```text
任意本机 ChatGPT ZIP 路径
  → 搜索 conversations*.json
  → 整体读取并 json.loads
  → mapping[].message 扁平化
  → 非文本 parts 转成文字或占位
  → message.metadata 整体写入 metadata_json
  → 可选读取 conversation_asset_file_names.json
  → 仅记录 ZIP 条目名、显示名、条目大小
  → 删除同名输出 SQLite（若已存在）
  → 新建 conversations/messages/assets/messages_fts
  → 写 CSV、JSONL 和 Markdown 报告
```

该路径的积极部分：

- 能识别 ChatGPT 分片会话 JSON；
- 保留 message metadata JSON；
- 生成可检索 SQLite 和辅助目录；
- 原始 ZIP 路径由用户显式提供，默认不联网。

主要问题：

- 没有 archive SHA-256、来源 manifest、adapter version 或批次 ID；
- 整体加载 JSON，没有成员数、展开大小、压缩比、JSON 大小或嵌套深度预算；
- 没有重复会话/消息规划和显式冲突对象；
- `messages.message_id` 没有数据库唯一约束，messages 也没有会话外键；
- 只保存一部分资产目录元数据，不复制 `.dat` 实体；
- 解析失败的资产 manifest 被吞成空映射，没有结构化 warning/error；
- 不保存受控的原始 ZIP 或只读副本；
- 输出 SQLite 已存在时直接删除，没有备份、审批或原子替换；
- 没有提交前/后验证、崩溃恢复和持久审计报告。

### 2.3 当前 ChatGPT 增量导入流程

```text
ChatGPT ZIP
  → 复用初始导入器的 load_conversations()/flatten_conversation()
  → 只读读取生产库当前 ID 和 MAX(conversations.create_time)
  → 已有 conversation_id：跳过整个会话
  → 新 ID 但 create_time <= cutoff：计数后跳过
  → 新会话内按 message_id / conversation_id+node_id 去重
  → 候选会话和消息必须严格晚于 cutoff
  → dry-run
  → SQLite online backup + integrity/count 校验
  → BEGIN IMMEDIATE
  → 只写 conversations/messages
  → 事务内与提交后验证计数、重复、孤立、FK、FTS 和保护表
```

该路径的积极部分：

- dry-run 与真实写入分离；
- 生产写入前使用 SQLite online backup；
- 单个 `BEGIN IMMEDIATE` 事务；
- 数据库在 dry-run 后发生可见状态漂移时拒绝旧计划；
- 检查全局重复消息 ID、孤立消息、FTS 行数和保护表指纹；
- 同一 ZIP 二次执行可以得到零候选。

当前不能接受为统一导入框架的原因：

- 不读取 `conversation_asset_file_names.json`，不清点、不复制、不验证资产；
- 已存在 conversation_id 时整个会话被跳过，不能表达新增消息、标题变化、内容变化或
  删除；
- “旧时间 + 新 ID”只计数并丢弃，当前测试还把它视为 `safe_to_import=True`，没有生成
  高风险冲突或进入审批；
- ZIP 内重复 message_id 按遇到顺序保留第一条，其余静默计数跳过，没有来源关系冲突；
- 只使用 ID 和时间，没有规范化内容哈希；
- 没有来源档案指纹、manifest、持久 plan ID、policy version 或 approval receipt；
- `--execute` 同时承担“请求执行”和“审批”，没有精确绑定计划内容；
- dry-run JSON 输出绝对 ZIP/DB 路径，缺少统一脱敏日志策略；
- 备份没有独立 manifest、SHA-256 receipt 和正式 restore 入口；
- 提交后验证若失败，只能报错，不能在同一协议内自动恢复主库；
- 没有导入批次状态、崩溃中间态或审计报告持久记录。

### 2.4 当前 DeepSeek 导入流程

DeepSeek 导入器读取一个结构化 JSON，把所有消息写入一个会话，固定：

```text
source_platform = deepseek
memory_space = whale
```

它优先用 `conversation_id + ordinal`，其次用 `conversation_id + message_id` 去重，并用
conversation upsert 刷新聚合字段。其问题是：

- 导入器自己执行 `CREATE TABLE IF NOT EXISTS`、`ALTER TABLE` 和 `PRAGMA journal_mode`；
- 直接连接目标库写入，没有备份、独立计划、审批和恢复；
- 没有检查已有同 ID 会话是否来自其他来源，upsert 可能把来源冲突变成覆盖；
- 没有来源文件哈希、原始记录定位、批次 ID、内容哈希或冲突记录；
- report 包含实际输入/数据库路径；
- `raw_json` 仅在消息输入带该字段时嵌入 metadata，不等于原始档案保留；
- 不处理媒体实体。

v8.3 不删除这条历史路径，但统一框架上线后，它必须停止直接写生产库。

### 2.5 当前备份、恢复和审计能力

| 能力 | 当前状态 | 结论 |
|---|---|---|
| 通用 `backup-real` | `shutil.copy2` 复制主 DB | 活跃 WAL 场景下不能单独作为一致性备份协议 |
| 增量导入备份 | SQLite online backup + integrity/count | 当前主库导入中最可靠，但缺 manifest/restore |
| 发布节点备份 | 主库逐文件复制，存在的 WAL/SHM 另行复制并核验 SHA-256 | 是人工发布流程，不是可复用导入事务 |
| 主数据库 restore CLI | 不存在 | 必须新增独立设计和演练 |
| 原始 ZIP 备份 | 不存在统一规则 | 当前 ChatGPT 原始包已缺失 |
| 导入批次报告 | 初始 Markdown、增量控制台 JSON、DeepSeek Markdown | 互不兼容，不能作为统一审计账本 |
| 回滚 | 增量事务提交前可 rollback | 提交后主库/实体联合回滚未实现 |

## 3. 初始导入与增量导入的主要差异

| 维度 | 初始 ChatGPT 导入 | 增量 ChatGPT 导入 |
|---|---|---|
| 目标 | 生成新输出目录和新 SQLite | 追加到现有生产 SQLite |
| 来源解析 | conversations + asset manifest | 仅 conversations |
| 会话策略 | 全量插入 | 已有 ID 整会话跳过；只接受严格较新会话 |
| 消息策略 | 全量插入，无显式全局去重规划 | ID/node 辅助去重，重复候选跳过 |
| 资产 | 写三字段目录，不复制实体 | 完全省略 |
| 原始数据 | 不保留原始 ZIP；另写派生 JSONL | 不保留原始 ZIP或规范化记录 |
| 数据库安全 | 可删除旧输出 DB；末尾 commit | 备份 + `BEGIN IMMEDIATE` + 验证 |
| 批次 | Markdown 统计，无 batch ID | dry-run/结果只打印，无持久 batch |
| 冲突 | 主要依赖 SQLite 异常 | 计数/跳过，不生成冲突对象 |
| 审批 | 无 | `--execute`，但不绑定不可变计划 |
| 恢复 | 无 | 提交前 rollback；提交后无 restore 协议 |
| 幂等 | 不保证；重建输出 | 同一已导入 ZIP 可得到零候选 |

两者不能继续作为两套独立生产路径。统一 ChatGPT Adapter 应对“全量”和“增量”只提供
不同扫描模式，输出同一种 manifest、规范化记录和导入计划；全局 planner 决定新增、
无变化、冲突或拒绝。

## 4. 当前数据模型、来源模型和批次模型

### 4.1 生产库真实结构

只读实测：

| 表 | 行数 | 导入相关结论 |
|---|---:|---|
| `conversations` | 409 | `conversation_id` 为主键；无来源 manifest/batch 外键 |
| `messages` | 16,324 | `message_id` 仅普通索引，数据库未强制唯一；无 conversation FK |
| `assets` | 172 | 只有 `asset_file/original_name/size_bytes` |
| `messages_fts` | 16,324 | 插入触发器维护；当前行数与 messages 一致 |
| `conversation_tags` | 2 | 私人整理层 |
| `conversation_marks` | 1 | 私人整理层 |
| `message_marks` | 0 | 私人整理层 |
| `memory_summaries` | 2 | 派生摘要；不属于来源事实 |

数据库实测同时表明：

- `PRAGMA user_version=0`，没有正式 schema 版本账本；
- 相关主表没有外键；
- 当前会话/消息重复 ID 组为 0，孤立消息为 0，integrity check 为 `ok`；
- `docs/schema.sql` 与生产真实 schema 不完全一致，不能单独作为迁移权威；
- 生产库没有 `memory_embeddings`，该结构属于其他阶段或 sidecar 设计；
- 当前没有任何 import/source/conflict/decision/verification 表。

### 4.2 当前来源字段

来源目前散落在：

- `conversations.source_file`
- `conversations.source_platform`
- `conversations.metadata_json`
- `messages.source_platform`
- `messages.metadata_json`
- 附件元数据中的来源 ID、URL 或平台字段
- `assets.asset_file`

当前生产分布：

| 记录 | `source_platform` | 数量 |
|---|---|---:|
| 会话 | NULL（历史初始 ChatGPT） | 177 |
| 会话 | `chatgpt` | 231 |
| 会话 | `deepseek` | 1 |
| 消息 | NULL（历史初始 ChatGPT） | 5,229 |
| 消息 | `chatgpt` | 10,407 |
| 消息 | `deepseek` | 688 |

读取层把空来源回退为 ChatGPT，这是兼容行为，不是来源档案证据。
`source_platform` 表示来源，`memory_space` 表示私人记忆作用域；两者必须继续分离。

### 4.3 当前批次证据

当前没有真正的 ImportBatch：

- 初始批次只留下 177 会话、5,229 消息、172 资产映射的处理报告；
- 增量批次只留下运行输出、备份和 changelog 中的
  231 会话、10,407 消息结果；
- DeepSeek 留下单会话导入报告；
- 没有统一 batch ID、archive fingerprint、adapter version、plan hash、审批人/审批动作、
  开始/结束时间、状态转换、警告、冲突、备份 ID、验证 receipt 或 rollback receipt。

### 4.4 原始数据保留

当前受控根中的唯一 ZIP 是处理后的成果包：

- 11 个成员；
- 只含 CSV、SQLite、JSONL、Markdown、PowerShell、Python 和 SQL；
- 不含 `conversations*.json`；
- 不含 `chat.html`；
- 不含 `conversation_asset_file_names.json`；
- 不含 `.dat`。

因此它不是原始 ChatGPT Source Archive。当前受控目录中 `.dat=0`，默认 ChatGPT 原始
ZIP 不存在，DeepSeek 默认原始 JSON/Markdown 也不存在。

## 5. 当前资产丢失根因

当前附件元数据 1,112 条的互斥来源分类：

| 分类 | 数量 | 根因 |
|---|---:|---|
| 初始来源字节历史存在、导入成果未保存 | 172 | 初始导入器读取 ZIP 条目大小并写目录，但不复制实体 |
| 初始附件元数据未映射、原始包缺失 | 176 | 初始 348 条附件中只有 172 条进入资产目录 |
| 增量导入省略资产清点、原始包缺失 | 764 | 增量导入器不调用资产扫描、不写 assets、不复制实体 |

由此：

```text
初始缺映射 176 + 增量缺清点 764 = 当前无 assets 行 940
已有 assets 行 172 + 实体字节 0 = entity_missing 172
```

另有 13 条附件声明大小与资产目录大小冲突，全部缺少实体字节，不能用哈希、文件头、
像素尺寸或实际文件大小判定任何一方正确，必须保持
`size_conflict_unresolved`。

根因不是前端不会显示附件，而是导入边界没有把“附件元数据”和“实体字节”放进同一个
可验证、可审批、可回滚的导入计划。v8.3 不能用空壳 API 掩盖该事实。

## 6. 统一导入流水线

目标流水线：

```text
Source Archive
  → Source Adapter
  → Validation
  → Normalized Records
  → Asset Inventory
  → Deduplication
  → Conflict Planning
  → Approval Gate
  → Atomic Commit
  → Verification
  → Audit Report
```

各层边界：

| 层 | 职责 | 禁止 |
|---|---|---|
| 原始来源数据 | 保存受控只读档案、中央目录、SHA-256 | 塞入 SQLite；Web 直接访问 |
| Source Adapter | 只解析来源格式并输出确定性记录 | 直接写生产库；决定全局去重 |
| 规范化中间数据 | 保存来源忠实且可追溯的统一记录 | 混入用户整理结论 |
| 生产来源事实 | 会话、消息、附件关系和来源绑定 | 静默覆盖；把推断当原始事实 |
| 私人整理层 | memory_space、标签、标记、项目、笔记、摘要 | 反向改写来源事实 |
| 派生索引 | FTS、语义、图谱、媒体只读索引 | 成为唯一事实来源 |
| 媒体实体 | 受控根中的已验证字节 | 任意 URL 下载；绝对路径身份 |
| 导入审计证据 | manifest、plan、审批、备份、验证、rollback receipt | 输出正文、私人路径或原始 ID |

生产 SQLite 不承担原始 ZIP 存档职责。原始档案、暂存记录和实体文件位于私有受控文件
根；生产数据库只保存稳定身份、相对 locator、内容摘要、状态和关系。

## 7. Source Adapter 契约

每个适配器至少暴露：

```text
source_type
source_version
archive_fingerprint
adapter_version

scan_manifest()
validate_source()
enumerate_conversations()
enumerate_messages()
enumerate_assets()
normalize_conversation()
normalize_message()
normalize_asset()
produce_import_plan()
verify_import_result()
```

语义约束：

1. `archive_fingerprint` 至少包含完整档案 SHA-256、字节数和中央目录摘要；不能由绝对
   路径、mtime 或文件名替代。
2. `scan_manifest()` 只读清点，不整体解压，不访问网络。
3. `validate_source()` 使用显式资源预算，输出结构化
   `info/warning/error/fatal`，不能吞掉解析失败。
4. `enumerate_*()` 顺序稳定；相同档案、适配器和策略必须产生相同输出。
5. `normalize_*()` 无副作用，不写生产数据库，不复制实体。
6. Adapter 只能解释来源格式和来源内部身份；跨批次、跨来源去重由统一 planner 决定。
7. `produce_import_plan()` 是来源侧候选描述；最终 plan 必须由统一 planner 与当前
   Core 快照共同生成。
8. `verify_import_result()` 只能验证该来源应产生的结果，不自行放宽全局验收。
9. 无法解析的数据进入 `unparsed_records`/warning，不得隐藏或伪装成空内容。
10. Adapter 不接受前端提供的根目录、数据库路径、任意 URL 或跳过审批参数。
11. 支持 dry-run、重复执行和取消；失败后不留下“completed”状态。
12. 适配器版本、规范化协议版本和策略版本都进入 plan hash。

ChatGPT 首版使用一个 Adapter，同时支持：

- `full_archive`：全量扫描，不等于全量覆盖；
- `incremental_archive`：仍全量清点来源包，由 planner 判断新增/相同/变化；
- `historical_backfill`：只对现有数据库建立可证明的历史绑定，不伪造缺失档案。

## 8. 统一规范化记录模型

所有记录使用版本化 canonical JSON 计算内容哈希。哈希输入必须固定字段顺序、UTF-8、
时间格式、数字表示和 null 语义。

### 8.1 `SourceManifest`

最小字段：

- `manifest_id`
- `manifest_schema_version`
- `source_type`
- `source_version`
- `archive_fingerprint`
- `archive_size`
- `archive_storage_ref`
- `central_directory_fingerprint`
- `adapter_version`
- `discovered_at`
- `retention_state`
- `validation_summary`
- `warning_count/error_count`

`archive_storage_ref` 是受控根 ID + 相对路径，不是绝对本机路径。

### 8.2 `ImportBatch`

- `batch_id`
- `manifest_id`
- `state`
- `attempt`
- `adapter_version`
- `normalization_protocol`
- `policy_version`
- `source_snapshot_hash`
- `production_snapshot_hash`
- `plan_id/plan_hash`
- `approval_id`
- `backup_id`
- `asset_staging_id`
- `started_at/updated_at/completed_at`
- `warning_count/error_count/conflict_count`
- `verification_id`
- `failure_code`

### 8.3 `NormalizedConversation`

- 来源类型、manifest、源原始 ID、批次、adapter version；
- 原始记录 locator；
- 标题和原始时间；
- 来源状态字段；
- 规范化内容哈希；
- message identity 列表摘要；
- warnings/conflict state；
- 最终 production conversation ID，可为空。

### 8.4 `NormalizedMessage`

- 来源类型、manifest、源 conversation/message/node ID；
- 批次和 adapter version；
- 原始记录 locator；
- role/author/content type/text/status/weight/时间；
- parent/children 的来源身份；
- 原始 metadata 的受控规范化表示；
- 内容哈希和结构哈希；
- warnings/conflict state；
- 最终 production message ID，可为空。

### 8.5 `NormalizedAttachment`

- 来源 attachment ID；
- 所属源会话和源消息；
- 原始文件名；
- 安全显示文件名；
- 声明 MIME、检测 MIME；
- 声明大小、实际大小；
- 内容 SHA-256；
- 来源 archive member locator；
- entity state/unavailable reason；
- `asset_entity_id`，可为空；
- warnings/conflict state；
- 最终 production attachment binding ID，可为空。

### 8.6 `AssetEntity`

- `asset_entity_id`
- `content_sha256`
- `actual_size`
- `detected_mime`
- `storage_root_id`
- `relative_entity_path`
- `first_seen_batch_id`
- `integrity_state`
- `created_at`

绝对路径不是身份。相同内容可共享一个逻辑实体，但每个来源附件关系仍单独保存。

### 8.7 `ImportConflict`

- `conflict_id`
- `batch_id`
- `record_kind`
- `source_identity`
- `production_identity`
- `conflict_type`
- `evidence_hashes`
- `risk_level`
- `allowed_decisions`
- `status`

### 8.8 `ImportDecision`

- `decision_id`
- `conflict_id` 或 plan scope
- `decision`
- `actor_kind`
- `policy_version`
- `evidence_hash`
- `decided_at`
- `supersedes_decision_id`

### 8.9 `ImportVerification`

- `verification_id`
- `batch_id`
- `phase`（precommit/postcommit/restore）
- `database_snapshot`
- `asset_snapshot`
- `expected_counts/actual_counts`
- `integrity_check/foreign_key_check`
- `index_checks`
- `source_binding_checks`
- `completed_state_check`
- `result`
- `verified_at`

每条规范化记录必须能够追溯来源类型、原始档案、原始 ID、批次、适配器版本、解析时间、
原始记录 locator、内容哈希、warning、冲突和最终生产 ID。缺失信息使用明确 null +
reason，不得伪造。

## 9. `ImportBatch` 状态机

允许状态：

```text
discovered
  → validating
  → planned
  → awaiting_approval
  → committing
  → verifying
  → completed | completed_with_warnings

任一未提交失败 → failed
已提交且经授权恢复 → rolled_back
崩溃边界不明 → recovery_required
```

规则：

- `batch_id` 唯一，不能来自绝对路径；
- 同一 `source_type + archive_fingerprint + adapter_version + policy_version` 的重复请求必须
  命中已有 manifest/batch 或生成明确 replay 计划；
- `completed` 必须同时证明数据库记录、资产关系、实体状态和索引验收；
- metadata-only 可以完成，但只能是 `completed_with_warnings`，并记录原因；
- `failed` 不能自动跳回 committing；重试增加 attempt 并重新验证；
- `recovery_required` 禁止新写入，直到完成受控恢复；
- `rolled_back` 必须有 restore/cleanup receipt，不能只改状态字符串；
- 所有状态转换追加审计，不覆盖历史。

## 10. 媒体资产保真

### 10.1 硬门禁

写数据库前必须完成 ZIP/目录资产清点。附件元数据和实体字节必须进入同一个 plan。

每个附件至少记录：

- 来源 ID、会话、消息；
- 原始文件名和安全显示文件名；
- 声明 MIME/检测 MIME；
- 声明大小/实际大小；
- SHA-256；
- 实体状态；
- 来源相对定位；
- 是否 metadata-only 及原因。

### 10.2 推荐目录边界

建议私有受控根：

```text
imports/
  source_archives/<source_type>/<archive_sha256>/source.<ext>
  ingestion_batches/<batch_id>/
    manifest.json
    normalized/
    plan.json
    decisions.jsonl
    audit/
  chatgpt/
    .staging/<batch_id>/
    entities/<batch_id>/
```

ChatGPT 实体首版继续位于既有 `imports/chatgpt/` 固定允许根之下，避免绕过 v8.2-E4-1A
路径门禁。未来其他来源必须增加固定、可信配置的 source root；不能把根目录变成 HTTP
或 CLI 任意参数。

### 10.3 复制与提交

1. 从已验证 archive member 流式读取到同卷临时目录；
2. 写入期间计算 SHA-256 和实际大小；
3. 复核文件头、MIME、声明大小、路径身份和检查前后 stat；
4. 全批次实体通过后，对批次目录执行同卷原子改名；
5. 数据库事务只引用已晋升且再次验证的相对路径；
6. 数据库失败时，新目录保持未引用状态并由 batch receipt 精确清理；
7. 回滚前先证明没有其他批次引用实体；
8. 不覆盖旧实体目录，不从外部 URL 自动下载。

内容相同的实体可由 `content_sha256` 去重；来源附件关系不能因实体去重而合并丢失。

### 10.4 metadata-only

允许以下情况：

- 原始档案没有实体；
- 原始档案已缺失；
- 外部 URL 被阻断；
- MIME/大小/哈希冲突；
- 不支持或危险格式；
- 用户明确选择不保留字节。

但必须保留确切 `entity_state` 和 reason。不得把 metadata-only 作为完整附件返回。

## 11. 去重与冲突策略

### 11.1 身份层次

优先级：

1. `source_type + source stable ID + source manifest lineage`；
2. 来源内部结构关系；
3. 版本化规范化内容哈希；
4. 时间、标题和文本相似度只作为辅助证据。

不得仅按正文文本去重，也不得把内容哈希当来源身份。

### 11.2 决策矩阵

| 场景 | 默认处理 |
|---|---|
| 同一 archive fingerprint 重放 | 幂等返回已有结果或零变更 plan |
| 同一会话 ID、结构和内容哈希一致 | `no_change` |
| 同一消息 ID、内容一致 | `no_change` |
| 同一消息 ID、内容变化 | 高风险 conflict，禁止自动覆盖 |
| 新消息出现在已有会话 | 计划为 append，验证父子关系和顺序 |
| 同正文、不同消息 ID | 保留两个来源记录，标记相似证据 |
| 标题/时间变化 | 显式 field conflict；按来源版本策略决定 |
| 来源中删除/缺失消息 | tombstone candidate，不自动删除生产记录 |
| 旧时间 + 新 ID | conflict，不再静默跳过 |
| ZIP 内重复 message ID | relationship conflict，不按遇到顺序静默取第一条 |
| 附件元数据相同、实体不同 | 高风险 asset conflict |
| 实体哈希相同、来源不同 | 共享 AssetEntity，保留全部来源关系 |
| 初始/增量重叠 | 由同一 planner 处理，不靠 cutoff 单独决定 |
| 不同来源疑似同一逻辑会话 | 只生成 link candidate，不自动合并 |

自动决策必须由版本化 policy 产生、确定性可重放，并保存证据哈希。高风险冲突进入
审批门禁。任何规则都不得静默覆盖现有生产数据。

### 11.3 哈希碰撞

若相同 SHA-256 对应不同大小或逐字节比较不一致：

- 立即 fail-closed；
- 标记 `hash_collision_or_corruption`；
- 隔离两个候选；
- 禁止实体共享、提交和自动恢复；
- 不在公开日志输出字节或私人名称。

## 12. 审批门禁

审批对象不是“这个 ZIP”，而是一个不可变的 `plan_id`。Plan hash 至少绑定：

- archive fingerprint；
- adapter/normalization/policy version；
- 生产数据库快照；
- normalized records 摘要；
- 资产清单和实体校验摘要；
- 新增/跳过/冲突/metadata-only/拒绝计数；
- 每个自动决策的证据；
- 备份和提交前置条件。

门禁规则：

- dry-run 不创建数据库备份、不写生产库、不晋升资产；
- 低风险纯新增可按产品策略允许一次显式批次批准；
- 任一内容变化、删除、旧时间新 ID、关系歧义、实体冲突或 schema 漂移都要求人工决定；
- approval 必须精确确认 plan ID，不能只传布尔 `--execute`；
- 执行前重新计算生产快照；不一致则 plan stale，重新规划；
- 审批 receipt 只记录身份和计数，不记录正文或绝对路径；
- Adapter 无权生成、伪造或绕过 approval。

## 13. 原子提交、回滚和崩溃恢复

### 13.1 单写者

- 同一时间只有一个合法 Core writer；
- 使用独占导入锁 + SQLite `BEGIN IMMEDIATE`；
- 移动端和普通 Web 继续只读；
- Adapter 永远不持有生产写连接。

### 13.2 提交顺序

```text
锁定 writer
  → 重新验证 plan 与 DB snapshot
  → 创建一致性 online backup + manifest + SHA-256 receipt
  → 完成并晋升已校验资产批次目录
  → BEGIN IMMEDIATE
  → 写 SourceManifest/ImportBatch/来源绑定/会话/消息/附件/实体关系
  → 更新 FTS 或记录待建派生索引
  → 事务内完整验证
  → COMMIT
  → immutable postcommit 验证
  → completed / completed_with_warnings
```

不能宣称文件系统与 SQLite 具备一个共同 ACID 事务。v8.3 的目标是：

- 数据库绝不引用未校验或未晋升实体；
- 提交失败只会留下可识别、未引用、可精确清理的新实体目录；
- 数据库事务失败不会留下部分业务记录；
- 崩溃后可由 batch manifest 判断“未提交、已提交或不确定”，不会猜测。

### 13.3 回滚

- 提交前异常：SQLite rollback，移除本批次未晋升临时目录；
- 资产已晋升但 DB 未提交：核验无引用后移除本批次新目录；
- DB 已提交、postcommit 验证失败：标记 `recovery_required`，停止所有新写入；
- 主库恢复必须使用精确 backup/manifest、文件身份、integrity/FK/schema/计数验证和显式
  restore ID；
- 恢复成功后再清理本批次独占实体，并写 rollback receipt；
- 不能静默用 `copy2` 覆盖活跃 DB；必须停止 writer，处理 DB/WAL/SHM，并演练恢复；
- rollback 不能删除复用实体或历史来源档案。

## 14. 原始档案边界

推荐规则：

- 进入扫描前，将用户批准的来源档案注册到受控原始档案区；
- 计算完整 SHA-256、大小和中央目录摘要；
- 可选择保留原始 ZIP，或保留不可变只读副本；选择必须由产品策略明确；
- manifest 始终保留，即使用户后来删除原始包；
- 完整 ZIP 不进入 SQLite；
- Web 前端和媒体 API 永远不能直接读取原始档案区；
- 原始包目录与实体服务目录分离；
- 原始包、normalized 中间数据和审计日志使用独立 ACL/加密/保留策略；
- 用户删除原始包后，manifest 标记 `source_archive_deleted_by_user`；已合法提交的生产
  记录不因此被静默删除，但未来重放/验证能力下降；
- 删除必须有精确目标、引用检查和 receipt。

当前尚未决定的保留期限、加密方式和磁盘配额见第 20 节。

## 15. 数据库边界与迁移结论

### 15.1 v8.3-0

**不迁移数据库。**

### 15.2 后续是否需要迁移

**需要。** 当前三张来源主表无法表达：

- 来源档案和 archive fingerprint；
- 导入批次和状态机；
- 来源原始 ID 到生产 ID 的多批次绑定；
- 规范化 attachment/message provenance；
- 冲突、决定、审批、验证和 rollback receipt；
- 媒体实体内容哈希、实际 MIME/大小、相对路径和状态。

建议加法式表：

- `schema_migrations`
- `source_manifests`
- `import_batches`
- `source_record_bindings`
- `attachment_bindings`
- `asset_entities`
- `import_conflicts`
- `import_decisions`
- `import_verifications`
- `import_state_events`

规范化大记录可以保存在批次私有目录或专用 ingestion staging store；生产主库只保存最终
来源绑定和审计摘要。若采用 staging sidecar，它不是第二生产 Core，不得被搜索/UI 当作
权威数据。

迁移约束：

- 先在生产库在线备份副本和历史 schema fixture 上验证；
- 只新增表、索引和可空列；不重写既有会话/消息 ID；
- 不要求重新导入才能继续使用现有 409/16,324；
- 不使 FTS、搜索、v8.2 移动端或只读 Web 失效；
- `source_platform` 与 `memory_space` 继续分离；
- schema version 从 `user_version=0` 进入显式版本账本；
- 是否增加 messages 的唯一索引/FK，必须先完成全量兼容和写路径测试；
- 迁移前后保留 DB/WAL/SHM 身份、备份、integrity、FK、FTS 和移动读取证据。

## 16. 历史兼容与 ChatGPT 适配器迁移

### 16.1 两条路径

```text
历史回填：现有数据库 → 可证明的 legacy provenance → 不改写现有业务 ID
未来导入：原始档案 → 统一 Adapter/Plan/Approval/Commit
```

### 16.2 历史回填

- 409 个会话和 16,324 条消息保持可用；
- 177/5,229 个来源为空的初始记录可标记为
  `legacy_chatgpt_inferred_from_initial_import_lineage`，但不能伪造 archive SHA-256；
- 231/10,407 个显式 ChatGPT 记录保留当前 ID；
- 1/688 个 DeepSeek whale 记录保持独立 provenance 和 memory space；
- 172 条 assets 保留为历史目录元数据，实体状态为 missing；
- 940 条附件保持 `asset_record_missing`；
- 13 条大小冲突保持 unresolved；
- 不伪造历史实体、不生成虚假 source archive、不重写消息 ID；
- 不要求重新导入才能使用搜索、摘要、标签、标记或移动端；
- FTS、语义、图谱和媒体索引是可重建派生层，不作为历史来源证据。

历史批次可建立一个明确的 `legacy_baseline` 记录，未知字段为空并带 reason。不得编造
看似真实的历史执行时间、适配器版本、档案哈希或审批。

### 16.3 ChatGPT 适配器迁移

1. 把 `load_conversations()`、message flatten 和 asset manifest 解析拆成无副作用
   Adapter；
2. 保留对 `conversations.json` 与分片文件的支持；
3. 替换“非文本转占位即完成”为结构化 content + attachment 关系；
4. 全量和增量都执行完整资产清点；
5. 不再使用单一 create_time cutoff 决定安全性；
6. 现有 ID 作为来源稳定 ID 候选，内容哈希作为变化证据；
7. 现有增量导入的 backup/transaction/verification 逻辑迁入统一 executor；
8. 初始导入器停止删除旧输出，改为 manifest/plan 驱动；
9. 上线统一入口后，将旧脚本改为兼容包装或明确 deprecated；
10. 在旧入口仍存在期间，禁止其绕过统一 writer 直接写生产库。

DeepSeek、Claude 等后续适配器只实现来源解析，不复制旧 DeepSeek 导入器的 schema 和
直写行为。

## 17. 安全威胁模型

| 威胁 | 基线控制 |
|---|---|
| ZIP Slip / 路径穿越 | 不整体解压；规范化 member；拒绝绝对、`..`、设备、UNC、ADS |
| symlink/junction/reparse 逃逸 | 检查 ZIP 属性、根和每级组件；resolved containment |
| 压缩炸弹 | 限制成员数、单成员/总展开大小、压缩比和 CPU/时间预算 |
| 超大 JSON | 成员大小门禁、流式解析或受限读取、取消和内存预算 |
| 深层嵌套 | 明确最大深度和节点数，超限 fatal |
| 恶意文件名 | NUL/控制字符、保留名、尾随点空格、Unicode 规范化和安全显示名 |
| MIME 欺骗 | 声明 MIME、扩展名、magic、实际内容分离；主动内容默认 attachment |
| 重复 ID | 来源内部、批次内、生产库三层检查；显式 conflict |
| 哈希碰撞/损坏 | 大小 + SHA-256 + 必要时逐字节复核；异常 fail-closed |
| 资源耗尽 | 批次预算、流式哈希、并发上限、取消、磁盘预留检查 |
| 外部 URL SSRF | 默认 metadata-only；无自动下载、代理或重定向 |
| 临时目录权限 | 私有 ACL、同卷、随机不可预测目录、禁止 Web 根可见 |
| 原始包泄露 | 独立私有根、加密/ACL、日志脱敏、Git ignore |
| 审计日志泄露 | 只记录 ID/哈希/计数/reason；不记录正文、URL、绝对路径 |
| 部分提交 | 资产先验证晋升；DB 单事务；状态机和恢复 receipt |
| 崩溃恢复 | durable manifest、事件日志、writer 锁、recovery_required |
| 重放导入 | archive/plan fingerprint 幂等识别 |
| 并发导入 | 单 writer + plan stale 检查 |
| 非法写入者 | 固定 executor、无 Adapter DB 权限、移动/Web 只读 |
| 备份不可恢复 | backup manifest、SHA-256、schema/integrity/FK/计数和 restore drill |

精确大小、压缩比、深度和保留预算必须配置化且进入 policy version；不能把示例阈值写死
成永恒安全值。

## 18. 测试矩阵

### 18.1 Adapter 单元测试

- ChatGPT 单文件/分片、空包、损坏 JSON、未知 source version；
- 重复 conversation/message/node ID；
- 无时间、非法时间、标题变化、内容变化、删除/缺失；
- asset manifest 正常、缺失、损坏、未映射、多映射；
- 相同输入跨进程输出相同 manifest/normalized hash；
- 未解析数据产生 warning/error，不被隐藏。

### 18.2 档案与资源安全

- ZIP Slip、绝对路径、编码/双编码穿越；
- symlink/reparse/junction、Windows 设备名、ADS、尾随点空格；
- 压缩炸弹、超大成员、超大 JSON、深层嵌套、成员数耗尽；
- MIME 欺骗、空文件、检查期间替换；
- 外部 URL 零请求；
- 私人路径、ID、URL 和正文不出现在报告。

### 18.3 规划、去重与冲突

- 同包重放；
- 同 ID 同内容、同 ID 内容变化、不同 ID 同正文；
- 现有会话追加新消息；
- 旧时间新 ID；
- ZIP 内跨会话重复消息 ID；
- 标题/时间变化；
- tombstone candidate；
- 实体哈希相同来源不同、元数据相同实体不同；
- 自动决策确定性和 plan hash 稳定。

### 18.4 批次和审批

- 全状态转换和非法转换；
- plan ID 精确审批；
- DB/asset/policy/adapter 漂移导致 stale；
- 未批准不可备份、不可晋升资产、不可写 DB；
- warning、conflict、metadata-only 计数绑定 approval；
- replay 返回已有 batch 或明确新 attempt。

### 18.5 原子性和恢复

- 资产复制中断、晋升前/后中断；
- backup 前/后、BEGIN 前/后、插入中、验证前、commit 前/后 failpoint；
- SQLite rollback 无部分记录；
- 未引用实体精确清理；
- postcommit 不一致进入 recovery_required；
- DB/WAL/SHM restore 演练；
- rollback 不删除共享实体；
- 连接句柄全部关闭，Windows 文件锁可释放。

### 18.6 数据库和历史兼容

- 从当前 409/16,324 的一致性副本执行加法迁移；
- 177/5,229 空来源历史记录不被误改写；
- DeepSeek 1/688 保持 `deepseek + whale`；
- assets 172、缺目录 940、冲突 13 状态不被美化；
- message/conversation ID、FTS 行数、搜索结果定位和 marks/summaries 不变；
- v8.2 移动端、real-readonly Web 和媒体索引回归；
- migration dry-run、备份、schema version、重复执行和 restore。

### 18.7 验收证据

- `py_compile`/静态分析；
- 定向 unittest；
- 完整 Python 回归；
- JavaScript 语法和现有浏览器回归；
- `PRAGMA integrity_check`、`foreign_key_check`、FTS/source binding 检查；
- DB/WAL/SHM 与资产根前后指纹；
- privacy scan；
- `git diff --check`；
- 真实生产只在批准节点使用受控副本验证后执行。

## 19. 推荐版本节点与验收门禁

### v8.3-0 — Unified Ingestion Baseline

交付：本文。
门禁：审计完整、边界明确、路线可执行；不写生产。

### v8.3-A — Source Manifest and Import Batch Core

交付：版本化 manifest、batch/event、policy、plan identity 和只读扫描 CLI。
门禁：无生产写入；同包 manifest 稳定；恶意档案资源门禁通过。

### v8.3-B — Normalized Records and Planning Engine

交付：九类规范化模型、canonical hash、去重、冲突和 plan。
门禁：旧时间新 ID、内容变化、删除、跨会话重复 ID 全部显式化；无静默覆盖。

### v8.3-C — ChatGPT Full/Incremental Adapter

交付：一个 ChatGPT Adapter 支持 full/incremental 扫描，旧脚本兼容迁移。
门禁：会话、消息、metadata 和资产 manifest 全量清点；Adapter 无生产 DB 权限。

### v8.3-D — Asset-preserving Import Pipeline

交付：受控原始档案、实体暂存、哈希/MIME/大小、原子晋升和 AssetEntity。
门禁：安全 fixture、附件 fixture、大文件 fixture；零路径逃逸、零外网、失败不报完整。

### v8.3-E — Conflict, Approval and Atomic Commit

交付：精确 plan 审批、单 writer、备份、事务、验证、恢复状态。
门禁：全部 failpoint；不能出现“DB 成功、资产失败却 completed”。

### v8.3-F — Database Migration and Restore Drill

交付：加法迁移、schema 版本、主库 backup/restore 协议。
门禁：当前生产副本迁移和恢复成功；移动/FTS/来源/私人整理层无回归。

### v8.3-G — Historical Backfill and Compatibility

交付：legacy provenance、172/940/13 状态回填、历史/未来双路径。
门禁：不伪造档案或实体，不重写现有 ID，不要求重新导入。

### v8.3-H — Final Acceptance

交付：完整安全、回归、恢复、性能、隐私和运行手册。
门禁：生产启用前至少完成一次用户批准的、带真实实体的受控 ChatGPT 导入；所有
验收 receipt 完整。

如果实现证明 C/D 或 E/F 无法安全分开，可以合并相邻节点，但不能删除其门禁。
DeepSeek、Claude 等具体 Adapter 不纳入 v8.3-0，也不应抢在 ChatGPT 首条统一链路验收
之前。

## 20. 未决产品决策

以下问题必须在相应实现节点前决定：

1. 原始 ZIP 默认是否永久保留；保留期限和用户删除语义；
2. 原始档案和实体的 Windows ACL、静态加密和密钥管理；
3. 原始档案、normalized records、审计和实体的磁盘配额；
4. 导入账本放入主 Core、专用 ingestion sidecar，还是文件 manifest + Core 摘要；
5. plan 审批使用 CLI、桌面 UI 或两者；actor identity 的粒度；
6. 何种低风险纯新增可以批次自动批准；
7. 跨来源会话只建立 link，还是允许人工 merge；
8. 用户删除来源档案后，已提交实体和生产记录的保留策略；
9. 内容相同实体跨来源共享的隐私和删除引用规则；
10. 外部 URL 是否永久 metadata-only，还是未来支持受控获取；
11. 首版单文件、总档案、压缩比、JSON 深度和批次资源预算；
12. 危险 MIME 的隔离、下载和未来预览策略；
13. 是否为 `messages.message_id` 增加唯一索引；
14. 是否为 messages/conversations/attachments 增加数据库外键；
15. legacy NULL `source_platform` 是否只读时推断，还是未来加法回填；
16. 主库 restore 由自动恢复还是人工确认的独立命令执行；
17. postcommit 验证失败时的最大自动处理范围；
18. 导入审计 receipt 的保留期限和导出格式；
19. 现有 DeepSeek 导入器何时禁用直写并迁移到统一 Adapter；
20. 真实媒体恢复所需的用户来源包托管和批准流程。

推荐默认：

- 原始档案先保留、可由用户显式删除；
- 外部 URL 保持 metadata-only；
- 高风险冲突全部人工审批；
- 主库 restore 必须独立精确授权；
- legacy 来源只做可证明回填；
- 任何阈值和自动决策都进入版本化 policy。

## 21. 最终 GO / NO-GO

### v8.3-0 设计基线

**GO**

理由：

- 当前入口、数据流、schema、来源、批次、资产、备份和恢复缺口已经形成可追溯证据；
- 1,112/940/172/13/0 的资产事实和根因已经明确；
- 统一 Adapter、规范化模型、批次状态、冲突、审批、资产保真、原子提交、恢复和安全
  边界已经定义；
- 现有 409 个会话和 16,324 条消息有清晰的无重写兼容路径；
- 实施节点和验收门禁可执行。

### 新生产导入、数据库迁移、媒体恢复和新来源 Adapter

**NO-GO**

在 v8.3-A 至至少 v8.3-F 的对应门禁完成前：

- 不得让旧 ChatGPT/DeepSeek 导入器新增生产写入；
- 不得迁移真实 SQLite；
- 不得恢复或伪造缺失媒体；
- 不得建立空壳媒体 API；
- 不得实现 DeepSeek、Claude 等新来源 Adapter；
- 不得绕过 plan/approval/backup/verification；
- 不得把 metadata-only 报告为完整附件。

v8.3-0 到此停止。
