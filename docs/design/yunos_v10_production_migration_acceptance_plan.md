# YunOS v10 — Production Migration and Acceptance Plan

## 1. 状态：DESIGN ONLY

本文属于 `v10.0 — Agency Design Baseline`，只定义 v10-F 的生产迁移、真实验收、恢复和封板
协议。**本节点不执行 production migration，不打开生产 writer，不停止服务，不修改五库或
assets，不创建备份，不运行 rollback/restore，不签发生产 Authorization Artifact。**

执行权只可能在 v10-A 至 v10-E 的实现、测试、故障注入、克隆门禁和恢复演练全部通过后，
由 v10-F 使用本次总任务明确授权进入。任何未知状态均 `fail-closed`。

## 2. 复用架构与非重复边界

v10 不创建第六数据库，不创建第二套 Runtime、Operational State、Evidence、Delegation、
Outcome、Approval、Conflict、Writer Lease、State Event 或 Source Anchor：

- Main DB：`data/ayunos_chatgpt.db`，历史聊天/内容权威源；
- Profile DB：`data/profile/approved_profile.db`，已批准画像；
- Semantic DB：`data/semantic/ayunos_semantic.db`，语义与长期记忆；
- Runtime DB：`data/runtime/ayunos_runtime.db`，v8 execution/receipt/usage/artifact/ledger；
- Operational State DB：`data/operational/ayunos_operational_state.db`，v9 当前状态并承载
  v10 Objective/Plan/Cognitive Run/Authorization/Tool/Verification/Learning Candidate 的加法式结构；
- assets：`data/ingestion_assets`，沿用 v8.3 资产树和 tree digest。

v10 初始 schema generation 预留为 `10001`。它必须是一个清晰、加法式、可重复识别的
migration；最终 migration id、DDL digest、source/target schema fingerprint 必须由正式
blueprint/实现确定，本文不写可执行 SQL，也不授权应用 migration。

## 3. 生产不变量

迁移前后均必须成立：

1. Main/Profile/Semantic/Runtime 的业务 schema 与逻辑内容不因 v10 migration 改变；
2. Operational State 只新增 v10 必需结构，不重建、不删除、不重命名 v9 表；
3. v9 event/history、projection、Now Package、Resume Package、Delegation、Outcome、
   Acceptance、writer lease 语义保持兼容；
4. 跨库只保存稳定引用与 digest，不建立跨数据库 foreign key；
5. writer 默认 off，迁移和验收后 active writer lease 为 `0`；
6. 生产服务恢复为 `real-readonly`，`readonly=true`，执行能力默认不可用；
7. 普通 Web/Tailnet/mobile 保持 GET-only，私人响应 `Cache-Control: no-store`；
8. Tailscale Serve 不获得 admin/write route，Funnel 保持关闭；
9. Search 模式继续直接读取本地 SQLite/FTS/Operational State，零 Provider 调用；
10. 旧 tag、历史、stash、远端均不变；不执行 v8302/Historical Reconstruction。

## 4. v10 加法式对象验收范围

正式 blueprint 和 migration 至少要能表达并审计：

- `objective_contracts`
- `cognitive_runs`
- `plan_graphs`
- `plan_nodes`
- `plan_edges`
- `capability_registry`
- `context_bindings`
- `authorization_artifacts`
- `tool_proposals`
- `tool_executions`
- `supervision_events` logical contract → 复用 `state_events`
- `verification_records` logical contract → 复用 `state_verifications`
- `learning_candidates` logical contract → 复用 `state_candidates`

实现必须先检查 v9 可复用表；能够通过稳定引用复用时不得复制 Delegation、Outcome、
Approval、Source Anchor、Conflict、Writer Lease、State Event、Verification 或 Candidate。
migration validation
必须拒绝 `DROP`、破坏性 `ALTER`、旧表 `DELETE/UPDATE`、未知 schema 覆盖和不同 digest 的
同 version migration。

## 5. 进入生产前的硬门禁

只有以下证据全部为 GO 才可创建 v10-F migration execution package：

- v9.0.1 起跑全量测试 `failed=0`、`errors=0`，日志及 SHA-256 可核对；
- v10-A 至 v10-E 节点实现、三轮内部复核和正式全量测试通过；
- JSON Schema、database、migration、projection、provider、policy、Tool Gateway、recovery、
  security、API、browser/mobile/PWA/offline 和 v7/v8/v9 regression 通过；
- DeepSeek 至少一次真实讨论和真实受控沙箱任务通过；Kimi 无密钥时仅契约/mock；
- Search 零 Provider execution/usage 证明；
- 原生 Agent Loop 完成沙箱写入、验证、acceptance/rollback；不依赖任意 shell；
- 所有越权、path/command injection、replay、expired authority、stall、crash、mobile/Tailnet
  写入场景 fail-closed；
- production writer boundary 已证明；若 Web 写入口不能证明 Tailnet 不可达，则正式采用
  local CLI/local broker；
- migration plan/schema/digest 固定，所有 production code 与测试树 clean；
- 可用磁盘空间、预计停机时间、operator checklist 和一次性 authority 已记录；
- 最终 tag 尚不存在，且本阶段仍不得创建它。

任何一项失败都不是“带警告通过”，而是 production migration `NO-GO`。

## 6. 迁移前证据快照

在停止竞争服务之前，先以严格只读方式记录：

### 6.1 Git 与服务

- HEAD、branch、权威旧 commit/tag object/peeled target；
- worktree、staging、untracked、stash 只读快照；
- loopback listener、服务 PID/command/children；
- `/api/status`、`/api/health`、service mode；
- Tailscale Serve 路由、Tailnet-only 状态、Funnel=`off`；
- 当前 HTTP method matrix、GET-only 与 `Cache-Control: no-store`。

### 6.2 五库物理与逻辑

对每库记录：

- resolved path、size、mtime、SHA-256；
- DB、`-wal`、`-shm` 是否存在、size、SHA-256；
- `PRAGMA user_version`、schema marker、managed schema fingerprint/digest；
- `PRAGMA integrity_check` 与 `PRAGMA foreign_key_check`；
- tables/indexes/triggers inventory 与 per-table row count/content digest；
- 关键逻辑摘要：Main conversation/message/assets，Profile approved state，Semantic records，
  Runtime execution/receipt/usage/ledger，Operational events/projections/objectives/plans/runs/
  delegations/outcomes/acceptance/writer leases。

只读访问导致的 SHM mtime 变化必须与内容变化分开判断；DB/WAL/SHM 的 size/SHA-256 是物理
内容门禁，mtime 只作为观测证据，不能被偷偷忽略或误报为写入。

### 6.3 Assets

记录 `data/ingestion_assets` 的文件数、总字节、逐文件相对路径/size/SHA-256、tree digest；
拒绝绝对路径、路径逃逸、reparse escape 和未绑定文件。

## 7. 五库与 assets 预迁移备份

使用现有 v9 release recovery contract 的演进实现，不另建恢复体系：

1. 为五个 SQLite 数据库创建一致性 online backup；
2. 将源 DB/WAL/SHM gates 作为 forensic evidence，不能把活跃 sidecar 当作 restore 输入；
3. 复制完整 `data/ingestion_assets`；
4. 生成脱敏 schema manifest、logical summaries、file hashes、asset tree audit、release manifest、
   recovery guide 和 provider configuration template（不含 secrets）；
5. 生成 manifest digest 与 backup-set digest；
6. 禁止正文、secret、Tailnet hostname/IP、无关绝对路径进入发布报告；
7. 在独立临时路径恢复全部五库/assets，先验证 package 真正可恢复；
8. 备份失败、空间不足、文件缺失、hash mismatch、integrity/FK failure 立即停止。

只有独立 restore 完全一致的 package 才能成为迁移 rollback source。失败 package 必须保留
`backup_accepted=false` 证据，绝不能冒充有效恢复点。

## 8. Disposable clone migration rehearsal

生产执行前必须用刚验收的备份建立可抛弃克隆，且连接字符串和 resolved path 明确不指向生产：

1. 克隆五库和 assets 到隔离根目录；
2. 只对克隆 Operational DB 应用唯一 v10 migration；
3. 校验 source fingerprint、migration id/version/digest、statement digest 和 target fingerprint；
4. 验证所有 DDL 为加法式，Main/Profile/Semantic/Runtime/asset tree 完全不变；
5. 运行 v9 Operational Store/API/Now/Resume/Delegation/Outcome/Acceptance regression；
6. 运行 v10 Objective/Plan/Run/Capability/Authorization/Tool/Verification/Learning tests；
7. 执行 projection rebuild，验证 event head、snapshot、projection digest 确定性一致；
8. 重复应用相同 migration 必须返回 `already_applied` 且不改变内容；
9. 相同 version 不同 digest、未知 schema/marker、高版本必须拒绝；
10. 产出 clone rehearsal manifest、日志、SHA-256 与明确 GO/NO-GO。

克隆 GO 不能替代生产前备份，也不能授权生产执行。

## 9. Failure injection

迁移克隆至少覆盖：

- migration 开始前、事务中、COMMIT 前异常，验证完整 rollback、无 marker 残留；
- COMMIT 后报告/进程崩溃，按 marker/schema reconciliation 判定 completed 或 recovery_required；
- disk full、backup failure、corrupt backup、manifest/backup-set digest mismatch；
- SQLite lock、竞争 writer lease、服务未停、WAL 非预期增长；
- source/target schema drift、缺 marker、同 version 不同 digest、未知对象/高版本；
- integrity/FK failure、projection corruption、event-chain mismatch；
- assets 缺失/篡改、restore path confusion、恢复连接生产路径；
- 服务恢复失败、GET-only/no-store 回归、Tailnet write/admin route 暴露、Funnel 打开；
- crash/restart 后已成功工具调用不重复，失效 authority 不恢复。

每个场景必须记录注入点、预期状态、实际状态、DB/WAL/SHM/asset gates、rollback/restore
结果、残留 PID/lock 和 `recovery_required`。同等失败最多一次调整后重试，不无限重跑。

## 10. Rollback rehearsal

rollback 只对 disposable clone 执行，禁止设计“反向删除 v10 表”作为生产常规回滚：

1. 迁移前 clone snapshot 为唯一 rollback source；
2. COMMIT 前故障依赖 SQLite transaction rollback；
3. COMMIT 后未知状态先 reconciliation，不能盲目重跑 migration；
4. 必须停止 clone writer/service，确认 lease 为零；
5. 从 verified backup 恢复到 sibling temporary path；
6. 完成五库/asset/manifest/digest/logic 验证后才替换 clone target；
7. 只清理该 clone 拥有的 WAL/SHM/journal 和进程；
8. 验证恢复后的 v9 schema、Now/Resume、搜索、Runtime、Operational State 全部一致。

任何不能证明一致性的 rollback 都标记 `recovery_required=true`。

## 11. 独立 restore rehearsal

正式 release backup 在独立路径和独立端口恢复，必须满足：

- 所有 resolved database/assets path 均位于 restore root，不连接生产；
- 五库 size/hash/schema/integrity/FK/logical digest 与 package manifest 一致；
- asset count/bytes/tree digest 一致；
- Objective、Plan、Cognitive Run、Delegation、Outcome、Verification、Acceptance 一致；
- Now Package、Resume Package、Search 查询结果一致；
- AI 只读页面可加载，但 write/admin capability 保持关闭；
- restored service 使用非生产端口，Tailnet/Funnel 均不配置；
- 测试后只清理 restore/rehearsal 自有路径和进程，不触碰生产/backup。

restore mismatch 直接使 backup 和 production migration NO-GO。

## 12. v9 regression gate

在克隆 migration、failure/rollback/restore 全部完成后，必须重跑 v9 兼容门禁：

- v7 memory/evidence/temporal retrieval；
- v8 Runtime execution/receipt/ledger/exactly-once/read-only status；
- v8 desktop/search/PWA/offline/private mobile；
- v8.3 ingestion schema/asset/read compatibility，不执行 v8302 或 Historical Reconstruction；
- v9 event/projection/freshness/Now/Resume/Delegation/Outcome/Acceptance；
- 简体中文界面和英文回退；
- Search 模式本地零 Provider 调用；
- 五库 integrity/FK 和 assets digest；
- GET-only、no-store、real-readonly、writer lease=0、Funnel off。

只有最终全量 `failed=0`、`errors=0` 才可进入生产窗口；既有环境型 skip 必须逐项解释。

## 13. 生产停机窗口与最终 preflight

正式执行前记录预计停机时间和正常时长。随后：

1. 精确识别并优雅停止竞争的 YunOS services/process tree；
2. 确认目标服务 listener 为零、无子进程、无 SQLite lock；
3. 确认所有相关 writer lease 为 `0` 且 writer default=`off`；
4. 确认五库/sidecar/assets 自备份以来未发生未解释变化；
5. 再次验证一次性 authority 的 objective/plan/migration/backup digests、有效期和权限；
6. 确认 `database permission` 仅允许 Operational DB additive migration；
7. 确认 Main/Profile/Semantic/Runtime/assets 无写权限；
8. 确认 merge/tag/push/network/next-version/persistent-agent 均为 false；
9. 确认 verified rollback package 本地可读且空间充足；
10. 任一条件不满足则恢复只读服务并停止。

`service_quiescence_evidence` 的固定 freshness TTL 为 120 秒，并在 control
preparation 与 production claim 两处独立强制执行。绑定同一 evidence digest 的
preproduction package build/verify/isolated restore、control preparation 与 execute
必须在一次连续、无人工停顿的流水线内完成；30 分钟 Authorization TTL 不会放宽这
个 120 秒门禁。若 evidence 过期，必须保留旧 evidence/package 作为审计记录，在新
路径重新 capture 和构建 package；不得覆盖产物、延长 TTL 或复用已拒绝的 controls。

## 14. 生产 migration 执行协议（仅供 v10-F）

本节是未来执行顺序，不在 v10.0 运行：

在 one-shot ledger claim 之前，执行器必须同时 pin 固定 control capture artifact、
capture controller、Operational DB 与受信任的 Tailscale executable。它先从同一组
handle 计算 source identities 并与 artifact 精确匹配；只有完全匹配后，才能在这些
handle 仍存续时执行固定的 `status → serve → funnel → serve` 只读探测，并从同一组
handle 再次复核 hash、路径和文件身份。任何不匹配都必须在 subprocess 与 ledger
claim 之前 fail-closed；不得通过放宽 Windows write/delete sharing 解决兼容问题。

1. 用受控一次性入口打开 Operational DB 的最短写窗口；
2. 获取单 writer lease，绑定 execution id、migration digest、authority digest；
3. `BEGIN IMMEDIATE` 后再次核验 source schema/marker；
4. 执行固定、typed、无动态 SQL 的加法式 migration；
5. 写 migration marker/schema digest，设置受控 generation；
6. 事务内验证 target fingerprint、对象数量、旧表不变量；
7. COMMIT；
8. 立即执行 integrity/FK/marker/schema/projection 验证；
9. 释放 writer lease并关闭 writer；
10. 写入 v8 Runtime receipt 和 v9 supervision/verification evidence；
11. 若 COMMIT 状态不明，执行 reconciliation，不自动重放；
12. 无法证明完成则 `recovery_required` 并按 verified backup 恢复。

迁移执行成功只表示 Tool executed/System verified，不等于用户已接受，也不自动完成项目。

## 15. 迁移后服务与网络验收

恢复生产服务后必须证明：

- 唯一本地服务绑定受控 loopback，`service_mode=real-readonly`；
- `readonly=true`、writer unavailable/default off、active writer lease=`0`；
- `/api/status`、`/api/health`、Search、v9/v10 只读 API 正常；
- 普通 Web、PWA、Tailnet、移动端仅允许 GET/HEAD；POST/PUT/PATCH/DELETE 被服务端拒绝；
- 私人 JSON/API 响应均 `Cache-Control: no-store`；
- Service Worker 不缓存 `/api/*`，离线内容清楚标为 cached/stale；
- Tailscale Serve 仍只把 Tailnet HTTPS 路由到只读服务；
- Funnel 明确为关闭，无公网 listener；
- Agency Admin Service 默认关闭、未被 Serve 暴露；
- 不能因代理来源表现为 loopback 而绕过本机写边界；
- 移动端无创建任务、授权、执行、accept high risk、Codex、merge/tag/push 操作。

必须从真实 Tailnet 路径做负向 write/admin 探测。只检查 UI 隐藏或 remote IP 不构成证据。

## 16. v10 真实接受矩阵

| 场景 | 必需证据 | 接受边界 |
|---|---|---|
| Search 本地模式 | 真实关键词结果；Runtime 无新 Provider execution/usage；移动/离线不回归 | 零模型调用、零费用 |
| Discussion | DeepSeek 真实调用、预算 context manifest、v7/v9 source anchors | 不创建执行委托、不改项目 |
| Plan | 真实项目 Objective Contract + Plan Graph + digest | 不执行变更 |
| Prepare | diff/authority/acceptance/rollback preview | 不正式写入 |
| Native Agent sandbox | 真实 Provider、typed tools、真实小变更、测试/diff/verification/rollback | 不污染生产仓库 |
| Codex Adapter | 支持接口则真实沙箱委托；否则 handoff-only/unavailable 证据 | 不伪造接入 |
| Overreach | 根外写、v10-G、shell、reset、merge/tag/push、secret、DB、network | 全部 fail-closed |
| Stall | 缩短测试时钟、suspected_stall、日志、owned child cleanup | 最多一次等价重试 |
| Restart recovery | checkpoint 恢复、receipt replay、expired authority refusal | 成功工具不重复 |
| Acceptance | Agent report/verification 后仍 waiting_acceptance | 仅有效 Acceptance Event 更新状态 |
| Learning | Fact/Observation/Workflow Candidate | 长期画像不变 |
| Provider switch | DeepSeek 可用；Kimi 无 key 安全 unavailable；手动切换 | 权限/状态不变，无隐藏切换 |

## 17. 迁移后五库和 assets 门禁

最终报告对每库列出：

- size、SHA-256、DB/WAL/SHM；
- schema version/marker/digest；
- integrity、foreign keys；
- writer/lease；
- events、objectives、plans、runs、delegations、outcomes、acceptance 逻辑摘要。

预期变化只能是：

- Operational DB 的 v10 加法式 schema/marker；
- 经真实验收明确产生且有 authorization/receipt/verification 的 v10 记录；
- Runtime DB 中真实 Provider/tool execution 的合法 receipt/usage/artifact/ledger。

Main/Profile/Semantic 内容、assets tree 和未经授权的 v9 状态不得变化。任何额外变化必须有
稳定来源、digest 和 authority；否则立即 NO-GO/recovery_required。

## 18. 最终 release backup 与恢复包

生产验收完成后，重新生成正式五库 release backup：

- 五个 DB 与可观察 sidecar gates；
- 完整 assets；
- provider configuration template（不含 secrets）；
- schema manifest、release manifest、logical summaries、hashes；
- recovery guide、节点/迁移/真实验收报告；
- backup-set digest。

再次在独立路径/端口 restore，验证五库/assets/Objective/Plan/Run/Now/Resume/Search/AI read-only
一致，证明不连接生产；清理仅限 restore 实例。正式恢复包验证失败则不得 main fast-forward/tag。

为避免报告引用自身所改变的 `backup-set digest`，正式包内冻结的 `release_reports/` 只收录
v10.0—v10-F 节点、迁移和真实验收报告；这些报告必须在 package seal 前完成。package seal、
独立 restore、`main --ff-only` 与 annotated tag 的最终封板报告放在 `docs/releases/`，作为受最终
Git commit 保护的包外 sidecar，引用已经封存的 manifest/digest。不得为了让报告包含自身 digest
而重建同一恢复包，也不得复制一份内容相同但路径不同的长报告。

## 19. 20 分钟进程协议

所有长任务记录 start time、PID、command、purpose、expected output、log、normal duration。接近
20 分钟检查 CPU、memory、stdout/stderr、log mtime、artifact growth、network wait、SQLite
lock、children、last completed step：

- 有明确进展：继续，并每 20 分钟再检查；
- 无实质进展：`suspected_stall`、保存证据、优雅终止、清理 owned children、诊断；
- 调整策略后最多一次等价重试；
- 不终止归属不明的外部进程，不无限重跑；
- 最终报告列出所有长任务、stall、重试和残留进程结果。

测试必须通过 fake clock/缩短阈值验证，不能真的等待 20 分钟。

## 20. Go/No-Go 与恢复决策

### GO

仅当备份、clone migration、failure injection、rollback、restore、v9 regression、生产 preflight、
生产 migration、五库/assets postverify、网络边界、真实场景、全量测试、最终恢复包全部通过，
且 `recovery_required=false`、writer lease=0、real-readonly、Funnel off，才可继续本地
`main --ff-only` 与 annotated tag 流程。

### NO-GO

以下任一项立即停止：

- backup 不可恢复；
- migration 无法 rollback/reconcile；
- schema/projection/restore mismatch；
- 五库损坏或出现未授权内容变化；
- writer lease/进程/锁不能清零；
- Web/Tailnet/mobile 写边界不能证明；
- Provider/Agent/verification/acceptance 真实闭环不能完成；
- 最终全量出现 failure/error；
- main 不能安全 fast-forward 或最终 tag 已存在。

生产 COMMIT 前失败优先事务 rollback；COMMIT 后未知状态先 reconciliation。只有 verified
backup 才可恢复。恢复后必须重新验证五库、assets、v9 regression、real-readonly、GET-only、
no-store、Tailnet 与 Funnel，并在 Operational State 中记录 `recovery_required`，不得伪造 GO。

## 21. v10.0 明确未执行

本设计节点没有执行：

- production migration、DDL 或任何生产数据库写入；
- 五库/assets 备份、clone、failure injection、rollback、restore；
- service stop/start、writer lease、Tailscale Serve/Funnel 修改；
- Provider 真实调用或 secret 读取；
- Historical Reconstruction、v8302、preserved_out_of_scope_engineering；
- v10-G、v10.1、v11；
- Git commit、merge、tag、push、rebase、stash 操作。

这些边界必须由 v10-F 的实际证据替换后，生产接纳才可能为 GO。
