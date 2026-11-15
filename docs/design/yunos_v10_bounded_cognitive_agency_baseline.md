# YunOS v10.0 — Bounded Cognitive Agency 设计基线

中文名称：YunOS v10 — 受限认知代理

开发分支：`feature/v10-bounded-cognitive-agency`

文档状态：`DESIGN ONLY`

初始 schema generation：`10001`

## 1. 设计结论

YunOS v10 在 v7 长期记忆、v8 Trusted Runtime 和 v9 Operational State 之上增加一层
受限认知代理能力。它把用户明确表达的目标转换为可解释、可验证的计划，由模型提出
结构化工具建议，由 YunOS 在独立授权、能力白名单和路径边界内执行，再通过监督、
验证和接受流程更新当前状态。

v10 不创建第六个数据库，不创建第二套 Runtime、Receipt、Ledger、Evidence、
Operational State、Delegation、Outcome、Approval 或 Writer Lease。v10 使用同一
Operational State DB 的加法式 extension marker `10001`；v9 core marker、事件 contract
和既有数据保持 `9001`。

本设计只允许以下节点：

`v10.0 → v10-A → v10-B → v10-C → v10-D → v10-E → v10-F`

v10-F 完成后停止。Agent、计划器、Provider 或工具均无权创建额外版本节点。

## 2. 起跑门禁

v10.0 只在
`docs/version_reports/yunos_v9_0_1_final_full_regression.md`
记录的第零门禁通过后成立。该报告确认：

- 权威 HEAD 为 `c6a5e2cdc59410f46c224a0cb79e8e5b417492e3`；
- annotated tag `v9.0.1-simplified-chinese-interface` peeled 到同一提交；
- 全量测试 1,495 collected、1,489 passed、6 个既有环境型 skip、0 failed、0 errors；
- 五个生产 SQLite 数据库及内容型 sidecar 前后未变化；
- Operational writer lease 为零；
- 生产服务保持 `real-readonly`；
- 未修改旧 tag、stash、Tailscale 或远端。

该报告是起跑证据，不是对任何 v10 实现或生产迁移的授权。

## 3. 正式定位与非目标

### 3.1 正式定位

YunOS v10 是以下能力的统一编排层：

> 目标理解、计划生成、能力选择、授权绑定、受控执行、过程监督、恢复、验证、接受和
> 分层反馈。

版本关系保持清晰：

- v7 回答“过去发生了什么、证据是什么”；
- v8 回答“模型调用和执行如何可信记录”；
- v9 回答“当前正在做什么、状态是否已接受”；
- v10 回答“在明确目标与授权边界内，下一步如何代表用户行动”。

### 3.2 非目标

v10 不是：

- 数字人格复制或自动修改长期人格；
- 无限自治、无人监督或常驻后台 Agent；
- 自动替用户决定人生方向、寻找工作或扩大目标；
- 第二个 Codex、第二套 Runtime 或通用企业工作流平台；
- 自动把一次行为写成稳定偏好；
- 自动 merge、tag、push、发布、付款或操作账号；
- 自动创建下一个产品版本；
- Historical Reconstruction、v8302 或
  `preserved_out_of_scope_engineering` 的重新启用入口。

## 4. 不可破坏的系统边界

1. 搜索模式完全本地，不调用模型、不产生 API 费用、不向外发送查询。
2. AI 模式只发送最小必要、可追溯、有预算的上下文。
3. Provider 只负责推理和提出建议，不是授权或安全边界。
4. 模型不直接拥有 shell、文件系统、Git、数据库、网络或凭据。
5. 所有 act 操作必须绑定有效 Authorization Artifact。
6. Tool executed、System verified 与 User accepted 是不同状态。
7. 普通 Web、Tailnet 和移动端保持只读。
8. 生产写入口默认关闭，优先使用本机 local broker。
9. 任何缺失结果、过期授权、恢复歧义或 ledger 不一致都 fail-closed。
10. v10-F 之后不得继续创建新版本。

## 5. 搜索模式与 AI 模式硬分离

### 5.1 搜索模式

固定链路：

```text
用户查询
→ 本地 SQLite / FTS / Operational State
→ 本地结果
```

搜索模式必须保留现有 `/api/search`、离线缓存、移动搜索、消息锚点和视觉行为。搜索
处理器不得导入或调用 Provider Registry、Runtime preview、Runtime execute、
Objective Contract 或 Agent Loop。搜索验收必须证明 Runtime 模型执行计数无增量。

### 5.2 AI 模式

固定链路：

```text
用户输入
→ Objective Contract
→ v7 Memory Package / Context Delivery
→ v9 Now Package / Project Resume Package
→ Context Binding 与预算
→ 手动选定 Provider
→ Answer / Plan / Tool Proposal
→ Authorization 与 Tool Gateway
→ Supervision / Verification / Outcome / Acceptance
```

AI 模式不得把缓存状态标成实时状态，不得发送完整数据库、无关私人状态、密钥、凭据
文件或未选择的历史内容。

## 6. 现有架构复用

### 6.1 v7 Memory 与 Evidence

复用：

- `Memory Package` 的 source-backed、只读证据契约；
- `Evidence Selection` 的 deterministic budget、selected/omitted ledger；
- `Temporal Memory Candidate` 的 candidate-only 边界；
- `Context Assembly` 的 approved profile、candidate、historical evidence 分层；
- `Context Delivery` 的 review、临时排除、source anchors 和 delivery digest。

v10 只保存稳定引用、digest、预算和 omission reason，不复制完整聊天或把实时状态写入
长期记忆。

### 6.2 v8 Trusted Runtime

复用：

- reviewed binding；
- request artifact；
- execution、run events、usage、failure state；
- receipt 与 append-only Runtime ledger；
- request/mutation idempotency；
- 单次 Provider send 和 in-doubt fail-closed。

所有真实模型调用继续进入 v8 Runtime。v10 不增加模型调用账本。

### 6.3 v9 Operational State

复用：

- `StateEvent`、`SourceAnchor`、projection、snapshot；
- `state_candidates`、conflict、approval、verification；
- writer lease、preview/commit、state receipt；
- `DelegationContract`、overreach event；
- `Outcome` 的 reported、verified、accepted 分离；
- Now Package、Project Resume Package；
- 用户接受后再应用完成候选的边界。

v10 的 Objective 必须绑定既有 project/work item；Cognitive Run 的审计事件绑定既有
work item 或 delegation，不扩展 v9 entity enum。

## 7. 两组控制维度

### 7.1 Agency Level

| 内部值 | 中文 | 允许 | 禁止 |
| --- | --- | --- | --- |
| `consult` | 讨论 | 分析、回答、只读上下文装配 | 正式委托、状态修改、写工具 |
| `plan` | 规划 | Objective Contract、Plan Graph、风险和验收 | 执行变更 |
| `prepare` | 准备 | 草稿、补丁预览、授权预览、只读检查 | 正式提交生产写入 |
| `act` | 执行 | 在有效 Authorization Artifact 内受控执行 | 越权、自我扩权、自我接受 |

Agency Level 只能由用户或显式预授权规则设定。模型和 Agent 不得自行提升。

### 7.2 Cognitive Stance

| 内部值 | 中文 | 作用 |
| --- | --- | --- |
| `continue` | 延续 | 优先复用已验证方法 |
| `explore` | 探索 | 允许新方法和新工具，但不扩大权限 |
| `breakthrough` | 突破 | 明确降低历史惯性并重新比较路径 |

Cognitive Stance 只影响方案排序，不能改变可调用工具、读写范围、风险上限或接受规则。

## 8. Objective Contract

每个 Cognitive Run 必须先形成不可变、版本化的 Objective Contract，至少包含：

- `objective_id`、`version`、`canonical_digest`；
- `user_utterance`、`interpreted_goal`、`project_id`；
- `agency_level`、`cognitive_stance`；
- `success_criteria`、`prohibited_outcomes`；
- `risk_tolerance`、时间和资源约束；
- `assumptions`、`ambiguities`、`clarification_required`；
- `context_requirements`、source anchors；
- `acceptance_policy`、`authority_request`。

澄清只在答案显著改变目标、风险、不可逆性、写入范围、账号/金钱/发布、价值冲突、
证据冲突或成功标准时成为硬门禁。其余必要读取、既有测试、Git 检查和授权内可逆修正
无需逐项询问。

高影响歧义进入 `waiting_authority` 或等价 waiting 状态；不依赖该答案的只读节点可以
继续。

## 9. Plan Graph

Plan Graph 是不可变、可验证、可修订的图，不是自由文本清单。每个 Plan Node 至少表达：

- node identity、title、purpose、dependencies；
- capability requirements；
- read/write scope；
- risk、reversibility、authority requirement；
- success criteria、verification、rollback；
- timeout、retry policy、state、evidence、omitted reason。

支持顺序、并行、决策节点、人工门禁、替代路径、回滚、暂停和恢复。Plan Revision 必须
生成新 revision，记录 change reason、authority impact、parent plan digest 和新 digest。

禁止：

- 修改根目标；
- 增加 Objective 外节点；
- 把延期工作提升成新版本；
- 绕过人工门禁；
- 把 Agent 自检 GO 当作接受。

## 10. Provider Registry 与模型调用

Provider Registry 的每个 profile 至少包含：

- `provider_id`、`display_name`、`endpoint`、`model`；
- capability flags、tool calling、structured output；
- context limit、timeout、reasoning options；
- cost metadata、enabled、secret reference、health state。

设计规则：

1. 用户手动选择 Provider 或使用可见默认，不允许隐藏切换。
2. Provider 切换不改变 Objective、Plan、Memory、Operational State 或权限。
3. DeepSeek 真实调用继续复用现有 D1 Runtime。
4. Kimi-compatible adapter 完成 profile、请求/响应契约和 mock；没有凭据时明确 unavailable。
5. secret reference 只指向受控私有配置，绝不保存 secret 值。
6. Provider 错误、结构化输出失败和预算耗尽都进入明确 failure state。
7. Provider output 永远视为不可信建议，不能生成授权事实。

现有 Runtime schema 固定 DeepSeek profile。因此 v10 首版可以实现模型无关的上层 Registry
和 adapter contract，但不得把真实 Kimi 调用伪装进 DeepSeek ledger。未来真实新 Provider
需要受控 Runtime schema 演进，不在 v10-F 封板范围内自动发生。

## 11. Capability Registry

每项 capability 至少记录：

- `capability_id`、display name、version；
- input/output JSON Schema；
- risk class、reversible；
- read/write scopes；
- required authority；
- timeout、max output；
- idempotency、verification method；
- availability、executor kind。

首批 capability 分为：

- 只读：目录、文件、文本、Git、数据库只读检查、允许的只读测试、浏览器只读检查、
  Memory、Operational State；
- 受控写：scope 内创建/修改文件、owned temp 删除、allowlisted command、测试、
  明确授权的 commit、owned change rollback。

Registry 是可审计描述；真正安全边界由 code-owned executor、Authorization Artifact 和
Tool Gateway 共同形成。数据库中的 capability 行不能自行授予执行能力。

## 12. Authorization Artifact

每个 `act` run 必须持有不可变 Authorization Artifact，至少绑定：

- objective digest、plan digest、project；
- read roots、write roots；
- allowed/denied capabilities、allowed commands；
- risk ceiling、max steps、max duration；
- max model calls、token/cost budget；
- commit/merge/tag/push/network/database permission；
- valid interval、rollback policy、acceptance policy；
- authority digest。

默认值：

- merge/tag/push/network/database write 为 `false`；
- next-version creation 和 persistent background agent 为 `false`；
- 授权过期、digest 漂移、计划修订影响权限或 scope 解析失败时立即失效。

生产 v10 Agent 不继承开发本身的工程授权。Provider、Agent、工具输出和缓存都不能签发
Authorization Artifact。

## 13. Tool Gateway

所有工具建议依次经过：

1. Schema validation；
2. Objective scope；
3. Plan node；
4. Authorization；
5. Risk policy；
6. Path normalization；
7. symlink/junction/reparse 检查；
8. command allowlist；
9. secret filtering；
10. preview；
11. execution；
12. output cap；
13. receipt；
14. verification；
15. state update。

模型只能提交 `capability_id + typed arguments`。executor 由代码生成 resolved argv，
默认 `shell=false`，不接受 shell string。必须拒绝管道、重定向、命令替换、未经授权的
环境变量、交互命令、隐式网络和无限进程。

路径策略必须拒绝 traversal、UNC、device path、ADS、drive switching、case/prefix
confusion、symlink/junction escape 和不属于本任务的 temp。所有写入必须位于规范化后的
授权根目录，并在执行前后重新验证真实文件身份。

## 14. 原生受控 Agent Loop

正式闭环：

```text
Objective
→ Context Binding
→ Plan
→ Tool Proposal
→ Policy Validation
→ Tool Execution
→ Observation
→ Replan
→ Verification
→ Outcome
→ Acceptance
```

模型提出工具建议，YunOS Tool Gateway 执行。每次 replan 都必须保持原 Objective，
生成新 Plan Revision，并重新判断 authority impact。达到 step/model/token/time budget、
发生 overreach 或结果不确定时停止，不得循环自我扩张。

## 15. Codex Adapter 审计策略

v10-D 只审计受支持、可脚本化的本机 Codex CLI 或正式 Agent 接口：

- 查看真实安装命令和本地帮助；
- 不凭记忆构造命令；
- 不调用桌面聊天窗口隐藏接口；
- 不逆向工程、不 UI 自动点击、不绕过登录或权限。

若存在可靠接口，Codex Executor 必须位于 Tool Gateway 后，绑定 Delegation Contract、
最小上下文、工作目录、命令白名单、timeout、日志和独立验证。若不存在，则标记
`unavailable` 或 `handoff-only`，生成 handoff artifact，由原生 Agent Loop 完成真实闭环。
Codex 不可用不阻塞 v10，但禁止伪造接入成功。

v10.0 当前只读观察已确认官方手册将 `codex exec` 标为 stable non-interactive
interface，并支持明确 cwd、sandbox、JSONL 和 output schema。但本机 WindowsApps 安装包内
的 `codex` / `codex.exe` 在当前执行令牌下均返回 `Access is denied`，bundled Python 环境
也未安装 `openai-codex` SDK。因此当前状态是 `handoff-only pending v10-D`，不是“已接入”。
v10-D 仍须用真实本地 `--help`、一次受控调用和独立验证才能提升为 available；不得为此
复制凭据、绕过 ACL 或自动安装依赖。

## 16. Supervision、20 分钟 stall 与 Recovery

Cognitive Run 至少支持：

- `queued`
- `reasoning`
- `planning`
- `waiting_authority`
- `executing`
- `waiting_external`
- `verifying`
- `waiting_acceptance`
- `completed`
- `failed`
- `cancelled`
- `recovery_required`

每次接近 20 分钟时检查 PID、CPU、内存、stdout/stderr、日志 mtime、artifact 增长、
网络等待、SQLite lock、子进程、交互输入和最后进展。无实质进展时：

- 记录 `suspected_stall`；
- 保存日志；
- 优雅终止 owned process tree；
- 诊断后最多一次等价但调整过的重试；
- 禁止无限重跑。

Recovery 从最后一个安全 checkpoint 恢复。已成功的 tool execution 根据 idempotency key
不再执行；失效授权不恢复；结果丢失或 send 状态 in-doubt 时 fail-closed；人工门禁保持
waiting；rollback 只处理本 run 拥有的变更。

## 17. Verification、Outcome 与 Acceptance

必须分别显示和记录：

1. Agent reported；
2. Tool executed；
3. System verified；
4. User accepted。

Verification 可以使用 diff、测试、Git、schema/hash、API、浏览器、独立查询或人工视觉。
工具退出码为零不是自动验收；模型声明完成也不是验收。

只有 Acceptance Policy 满足后，v9 当前状态才可更新。自动接受仅限用户预先明确授权、
低风险、可逆、确定性验收、无冲突、无越权且无未解决警告的结果。Agent 不能接受自己
的结果。

## 18. Learning 边界

执行后固定链路：

```text
Outcome
→ Fact Record
→ Observation
→ Pattern Candidate
→ Workflow Candidate
→ 用户批准或多次证据
→ v7 长期记忆候选
```

一次行为不能直接成为稳定偏好；一次失败不能成为永久禁用规则。Learning Candidate
复用 v9 `state_candidates`，始终显示 candidate-only 信任状态，不直接写 approved profile。

## 19. 存储架构

### 19.1 固定决策

- 不创建第六个数据库；
- 使用 `data/operational/ayunos_operational_state.db`；
- 保留 `operational_schema` core marker `9001`；
- 新增 `cognitive_schema` extension marker `10001`；
- 不修改旧 `state_events`、entity enum 或历史记录；
- 跨库只保存稳定引用和 digest，不建立跨数据库 Foreign Key。

### 19.2 最小新增结构

新增：

- `cognitive_schema`
- `objective_contracts`
- `cognitive_runs`
- `plan_graphs`
- `plan_nodes`
- `plan_edges`
- `provider_registry`
- `capability_registry`
- `context_bindings`
- `authorization_artifacts`
- `tool_proposals`
- `tool_executions`

复用：

- supervision → `state_events`
- verification → `state_verifications`
- learning → `state_candidates`
- approvals → `state_approvals`
- delegation/outcome/acceptance → v9 既有结构
- model execution/usage/receipt → v8 Runtime

生产 migration 必须在克隆、failure injection、rollback、restore 和 v9 回归通过后，以
单事务创建 extension、写 marker、重算 schema digest。失败时回滚事务或恢复正式备份。

## 20. 本地写入、Web、Tailnet 与移动端

普通 Web 与 Tailnet 保持 read-only。`127.0.0.1:8765` 的 `real-readonly` 服务继续作为
Tailscale Serve 唯一目标。

写入优先使用默认关闭的 local broker：

- 独立 loopback 端口或本地 CLI；
- 不配置到 Tailscale Serve；
- 短期 capability；
- 独立 HttpOnly session 与 CSRF；
- Host、Origin、session、capability 共同验证；
- 每次任务临时启用，结束后关闭。

不能只根据 remote IP、User-Agent、隐藏按钮、前端变量或单一 Origin 判断本地。如果
无法证明 Tailnet 不能访问写入口，则不开放 Web 写入，改用 local CLI/broker。

移动端仅允许查看 Objective、Plan、进度、Agent、overreach、verification 和 waiting。
不得创建任务、授权、扩大权限、接受高风险结果、调用写工具、启动 Codex 或执行
merge/tag/push。

## 21. AI 认知任务工作区

YunOS 保留极简首页和 Omnibox 的“搜索 / AI”，不增加独立 Agent 首页模式。

AI 任务进入“认知任务工作区”：

- 顶部：标题、项目、状态、Provider、Agency Level、Cognitive Stance、风险、最后活动；
- 主文档：目标理解、成功标准、明确不做、个人上下文、计划、决策点、当前执行、
  验证、结果、学习候选；
- Prepare/Act 右栏：Agent、读写范围、允许工具、禁止操作、Git 权限、预算、运行时间、
  stall、overreach；
- 结果区：Agent 报告、系统验证、未解决问题、影响、回滚、当前判定、等待接受。

默认简体中文，无翻译时回退英文；内部 schema/API 保持英文。移动端隐藏全部写控件，
同时服务端不存在对应远程写路由。

## 22. Threat Model

| 风险 | 触发 | 预防 | 检测 | 恢复 / fail-closed | 测试 |
| --- | --- | --- | --- | --- | --- |
| Prompt injection | 用户内容要求越权 | 内容与 authority 分层；模型仅提案 | proposal/plan scope diff | 阻断并记录 overreach | 恶意聊天指令 |
| 历史恶意命令 | 检索命中旧命令 | 历史标为 evidence，不作控制指令 | context source 分类 | 丢弃工具意图 | 注入历史样本 |
| 工具输出注入 | stdout 含“继续执行” | observation 数据化，不回灌为 authority | output classifier | 保留证据但不执行 | 恶意测试输出 |
| 伪造工具结果 | 模型声称执行成功 | Tool Gateway 独立 receipt | receipt/run id 校验 | unverifiable/failed | 假 receipt |
| 伪造用户授权 | 模型生成授权 JSON | 仅可信 issuer 可签发 | authority digest/issuer | waiting_authority | agent-issued artifact |
| 扩大计划 | replan 增加根目标外节点 | Objective/parent digest 绑定 | revision scope diff | 拒绝 revision | 创建额外版本 |
| 请求秘密 | proposal 读取 key/env | secret scope 永久 deny | secret filter/audit | blocked，无内容返回 | key/path/env 请求 |
| 路径逃逸 | `..`、UNC、ADS、drive switch | 原始路径和真实路径双校验 | path policy reason code | 不执行 | 编码 traversal 矩阵 |
| symlink/junction | 授权内链接到外部 | 逐组件 reparse 检查 | 执行前后身份复验 | 停止并 recovery_required | symlink/junction fixture |
| 任意 shell | 模型提交 command string | typed capability、code-owned argv | schema/allowlist | 拒绝 | `&&`、pipe、redirect |
| 命令注入 | 参数含控制符或选项逃逸 | typed schema、固定 argv 位置 | resolved argv audit | 拒绝 | 参数 fuzz |
| 输出注入 | HTML/ANSI/控制字符 | 转义、截断、artifact 存储 | renderer/secret scan | 安全摘要 | XSS/ANSI 样本 |
| 重复调用 / replay | 重发 proposal 或恢复 | idempotency key + unique binding | execution lookup | 返回既有结果 | crash/replay |
| 授权过期 | 恢复时超过 expires_at | 每步实时验证 | expiry event | waiting_authority | 缩短时钟 |
| 预算失控 | 无限 replan/model calls | step/time/token/cost hard limit | budget telemetry | failed/waiting | budget exhaustion |
| 无限循环 | 节点互相反复 | DAG/revision/attempt 上限 | repeated-state detector | 终止 | cyclic plan |
| 卡死 | 无日志或 artifact 增长 | timeout 与 20 分钟检查 | PID/CPU/mtime | 终止 owned process，最多一次重试 | 缩短测试时钟 |
| 进程残留 | cancel/crash 后子进程存活 | owned process tree | post-run inventory | 优雅终止并核对 | child process fixture |
| 网络外传 | 工具隐式联网 | network=false 默认；无下载 capability | socket/process audit | 阻断 | 网络调用模拟 |
| Provider 故障 | timeout/HTTP/配额 | 手动 provider、timeout、无隐藏切换 | Runtime failure state | 明确失败，不自动切换 | mock provider failures |
| 错误结构化输出 | JSON/schema 不匹配 | strict schema | validation error | 不执行，允许受限重新规划 | malformed payload |
| 上下文过量 | 选择过多私人内容 | v7 budget/omitted ledger | char/token/source counts | 缩减或停止 | hard budget |
| 过期状态 | stale Now/Resume | freshness 与 live binding | timestamp/source check | 标记 stale，必要时重取 | stale fixture |
| 缓存误导 | 离线结果标实时 | API network-only、cache label | cache classification | 只读 stale 展示 | offline/PWA |
| Runtime success 误作接受 | Provider 成功返回 | Outcome/Verification/Acceptance 分层 | lifecycle invariant | waiting_acceptance | 未接受场景 |
| 误写长期画像 | 单次结果直接升格 | 只生成 state candidate | profile diff | 阻断 profile write | learning candidate |
| 移动端越权 | 移动请求写 API | 远端无写路由 | request-method audit | 403/404 | GET-only browser |
| Tailnet 写入 | Serve 代理到写入口 | 8765 read-only；broker 独立且未 Serve | Serve target/route audit | 关闭 broker | Tailnet mutation probe |
| CSRF | 本地浏览器被跨站触发 | session + token + Origin/Host | CSRF failure log | 403 | cross-origin POST |
| schema drift | 未授权表/trigger 变化 | 双 marker 与 schema digest | startup fingerprint | 服务 unavailable | drift injection |
| projection corruption | current state 与事件不符 | deterministic rebuild | digest comparison | rebuild clone/restore | corruption fixture |
| backup failure | 迁移前备份不完整 | 五库 online backup + manifest | hash/integrity/FK | 禁止迁移 | missing/tampered backup |
| restore mismatch | 恢复结果不等价 | 独立路径恢复与逻辑摘要 | backup-set digest | recovery_required | restore rehearsal |
| Agent 创建下一版本 | proposal/plan 出现额外节点 | permanent denied action | overreach event | waiting，项目不更新 | version-overreach 场景 |

## 23. 验收与生产迁移计划

v10-F 前必须在 disposable clone 完成：

- Objective、Plan、Prepare、只读执行、沙箱写入；
- 真实 DeepSeek、Kimi unavailable/mock；
- overreach、路径、命令、网络、数据库和版本阻断；
- stall、cancel、crash、idempotent recovery；
- verification/acceptance/learning 边界；
- desktop/mobile/PWA/offline/Tailnet；
- migration failure、rollback、restore、旧功能回归。

生产 migration 前必须备份五库、sidecars、assets，记录 schema/logical digest、停止竞争
服务、确认 writer lease 为零并预计停机时间。迁移后验证双 marker、integrity/FK、
events/projections、writer off、real-readonly、Tailnet、Funnel off、GET-only 和旧功能。

任何失败均 `NO-GO`、回滚或恢复，不能用报告措辞替代证据。

## 24. 唯一版本路线

| 节点 | 交付 |
| --- | --- |
| v10.0 | 设计、ADR、schema contract、DESIGN ONLY blueprint、Threat Model、迁移与验收计划 |
| v10-A | Objective Contract 与 Intent Engine |
| v10-B | Plan Graph 与 Capability Engine |
| v10-C | Provider Registry 与 Cognitive Orchestration |
| v10-D | Tool Gateway、原生 Agent Loop、Supervision 与 Recovery |
| v10-E | 简体中文认知任务工作区与只读移动端 |
| v10-F | 生产迁移、真实验收、恢复包、全量回归、本地封板 |

不允许插入、派生或自动创建其他节点。

## 25. 三轮内部 Gate

### 25.1 架构 Gate

- v7 Evidence 不重复：通过设计；
- v8 Runtime/Receipt/Ledger 不重复：通过设计；
- v9 State/Delegation/Outcome/Approval 不重复：通过设计；
- 不创建第六库：通过设计；
- Search 不变成 AI：通过设计；
- Provider 不作为安全边界：通过设计；
- 不形成无人监督持续 Agent：通过设计；
- 路线只含 v10.0 至 v10-F：通过设计。

### 25.2 安全 Gate

本基线已定义 injection、overreach、path/shell/replay/expiry/stall/crash/restore、网络、
Provider、移动、Tailnet、CSRF、schema/projection、backup/restore 和 next-version 的
fail-closed 与测试要求。实现阶段必须逐项生成可复现证据，设计自审不能替代运行验证。

### 25.3 产品 Gate

- 默认简体中文、英文回退；
- 保留极简首页和 Search/AI Omnibox；
- AI 使用文档式任务工作区；
- 权限、计划、验证和等待接受对用户可见；
- desktop 是控制台，mobile 只读；
- 不把桌面改造成通用管理后台；
- 离线状态不伪装为实时。

## 26. 已知限制

1. 当前生产 Runtime SQL contract 固定 DeepSeek；Kimi 无凭据时只能做 adapter contract、
   mock 和诚实 unavailable。
2. 仓库尚无原生 Tool Gateway、Cognitive Run 或 Codex Executor 实现。
3. 当前 `runtime-admin` 没有足以证明 Tailnet 隔离的完整 session/CSRF 边界，不能直接
   复用为项目 writer。
4. Operational State core marker 与 `StateEvent` contract 固定 9001，v10 必须使用
   extension marker，而不是全局改号。
5. Windows 无 symlink privilege 的环境只能运行相应拒绝逻辑的替代测试；正式安全验收
   仍需在可创建 symlink/junction 的隔离环境补证。
6. 本文是设计基线，不代表 schema 已迁移、Provider 已调用、Agent 已执行或生产已接纳。

## 27. v10.0 设计判定

本基线完整定义了 v10 的范围、复用关系、安全边界、唯一路线和验收规则。只有关联 ADR、
设计关闭报告、JSON Schema、DESIGN ONLY SQL blueprint 及静态验证全部通过后，主执行
窗口才可把 v10.0 判为 GO 并进入 v10-A。
