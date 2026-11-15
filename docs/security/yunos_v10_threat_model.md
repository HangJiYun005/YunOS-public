# YunOS v10 — Bounded Cognitive Agency 威胁模型

## 1. 文档状态与边界

本文是 `v10.0 — Agency Design Baseline` 的 **DESIGN ONLY** 安全基线。它定义 v10-A 至
v10-F 必须实现和验证的控制，不授权生产写入、生产 migration、Provider 密钥变更、
Tailnet 写入、`merge`、`tag`、`push` 或任何下一版本创建。

v10 是 v7 Memory/Evidence、v8 Trusted Runtime 和 v9 Operational State 之上的受限认知代理层：

- 复用 v7 的 Memory Package、Evidence Selection、Temporal Memory 和 source anchor；
- 复用 v8 的 execution、receipt、usage、artifact、ledger、failure state、exactly-once；
- 复用 v9 的 Objective/Project/Delegation/Outcome、state event、projection、conflict、
  freshness、writer lease 和 acceptance boundary；
- 只对 Operational State DB 做加法式扩展，不创建第六数据库；
- AI 只提出 `Tool Proposal`，真正执行只能由 YunOS `Tool Gateway` 完成；
- Search 模式完全本地、零模型调用；AI 模式才允许受预算的 Provider 调用；
- 普通 Web、Tailnet、PWA、移动端保持只读。生产写通道必须是本机独立受控入口，
  无法证明 Tailnet 不可达时退化为 local CLI/local broker。

本文不把 Provider 当作安全边界，不信任模型文本、聊天历史、工具输出、缓存、
Runtime success 或 Agent 自报结果。

## 2. 资产、主体与信任边界

受保护资产包括：五个生产 SQLite 数据库及 sidecar、`data/ingestion_assets`、v7 记忆、
v8 Runtime ledger/receipt、v9 当前状态、Provider secret reference、Authorization Artifact、
Objective/Plan canonical digest、Capability Registry、Tool Gateway、用户接受事件、发布历史与
旧 annotated tags。

主体与边界：

1. 用户输入、历史聊天、Memory Package、Provider 输出和工具输出均为不可信内容；
2. `Objective Contract`、`Plan Graph`、`Authorization Artifact` 是相互绑定但职责不同的控制对象；
3. `Tool Gateway` 是模型建议与真实能力执行之间的强制边界；
4. v8 Runtime 是调用和执行证据账本，不是授权签发者，也不是用户接受者；
5. v9 Operational State 是任务状态和接受状态的权威存储，不是模型权限来源；
6. desktop local admin、普通 loopback Web、Tailscale Serve、Tailnet mobile 是不同信任域；
7. 文件根目录、SQLite 文件、进程、网络和 Provider 均须显式授权，默认拒绝。

## 3. 全局安全不变量

- `Agency Level` 不得由 AI 自行提升；`Cognitive Stance` 不扩大任何权限。
- 每个 `act` run 必须绑定未过期的 Authorization Artifact，且 objective/plan/authority digest
  完整匹配。
- 模型永远不拥有任意 shell、任意文件系统、任意 Git、任意 SQL、任意网络或凭据。
- 工具输入必须 typed；默认 `no shell`；命令使用模板和 resolved argv。
- `merge=false`、`tag=false`、`push=false`、`network=false`、`database write=false`、
  `next-version creation=false`、`persistent background agent=false` 为生产默认值。
- Agent reported、Tool executed、System verified、User accepted 四种状态严格分离。
- 一次行为只可生成 Fact/Observation/Workflow Candidate，不得直接改写长期画像。
- 所有未知状态、证据缺失、digest 不匹配和恢复歧义均 `fail-closed`。

## 4. 完整威胁清单

### T01 — Prompt injection

- **风险：** 用户内容或外部文本诱导模型忽略 Objective、扩大权限、泄漏数据或伪造完成。
- **触发：** 输入包含“忽略规则”“直接执行”“把秘密发出”等指令，或内容与授权冲突。
- **预防：** 内容与控制面分离；系统生成的 Objective/Plan/Authorization 不从自由文本提升权限；
  Tool Gateway 独立校验全部调用。
- **检测：** 记录 proposal 与 authority 差异、敏感意图和 policy reason code；触发
  `agent_overreach`/injection observation。
- **恢复：** 丢弃未执行 proposal，保留审计证据，重新装配最小上下文；已开始的 run 回到最近安全 checkpoint。
- **fail-closed：** 冲突或来源不可信时不执行、不提升 Agency Level、不更新完成状态。
- **测试：** 在 user utterance、memory 和 retrieved document 中注入越权指令，证明所有写、网络、
  secret、Git proposal 均被拒绝。

### T02 — 聊天历史中的恶意命令

- **风险：** 历史消息被误当作当前用户授权或当前计划节点。
- **触发：** Memory Package 含旧 shell/Git/数据库命令、过期批准或另一项目的权限声明。
- **预防：** 历史只作为带 source anchor 的不可信事实；当前 Authorization Artifact 必须独立签发并绑定当前 digest。
- **检测：** context binding 标记来源、时间、项目和信任等级；检测历史命令型文本。
- **恢复：** 移除污染 context binding，重新生成 context digest 与 plan revision。
- **fail-closed：** 历史文本不能创建 authority；当前证据不足时进入 `waiting_authority`。
- **测试：** 注入带“已批准 push/删库”的旧对话，验证权限仍为默认拒绝。

### T03 — 工具输出中的指令注入

- **风险：** 文件、测试日志、网页或数据库结果中的文本诱导模型发起新动作。
- **触发：** observation 包含伪造的系统指令、授权或后续命令。
- **预防：** observation 数据与控制指令分离；工具输出只满足既有 Plan Node，不自动生成权限。
- **检测：** 对输出做 injection pattern/secret scan，标注 `untrusted_tool_output`。
- **恢复：** 截断或隔离污染 artifact，使用摘要和 digest 重新观察。
- **fail-closed：** 工具输出不能改变 objective、plan、authority 或 acceptance。
- **测试：** `read_file`/browser/test 输出恶意“执行任意 shell”，验证 proposal 被 policy 拒绝。

### T04 — 模型伪造工具结果

- **风险：** 模型声称文件已写、测试已过或状态已改变，而实际没有执行证据。
- **触发：** response 中出现无对应 tool execution/receipt 的成功声明。
- **预防：** 只有 Tool Gateway 创建 execution；verification 必须独立查询真实目标并绑定 receipt digest。
- **检测：** 对 Agent report、execution、verification 三方做引用一致性检查。
- **恢复：** 将 run 标记 `verification_failed`，补做独立验证或重新执行未完成节点。
- **fail-closed：** 无 execution receipt 不得标记 Tool executed；无 verification 不得完成。
- **测试：** Fake Provider 返回虚构 tool result，确认系统状态仍未执行、未验证、未接受。

### T05 — 模型伪造用户授权

- **风险：** 模型用自然语言声称用户已授权高风险操作。
- **触发：** Tool Proposal 含伪造 authorization id、扩大 scope 或“用户已同意”文本。
- **预防：** Authorization Artifact 只能由受信入口建立，使用 canonical digest、有效期和任务绑定。
- **检测：** authority lookup、digest、project、scope、expiry、nonce 全量校验。
- **恢复：** 拒绝 proposal，写 overreach event，等待真实授权。
- **fail-closed：** 缺失、未知、伪造或不匹配 authority 一律 `waiting_authority`。
- **测试：** 伪造 authority JSON/id/digest，逐项验证拒绝且无工具执行。

### T06 — 模型扩大计划

- **风险：** Agent 增加 Objective 外节点、扩大项目、创建长期后台任务或开始下一版本。
- **触发：** Plan Revision 新增无 Objective 依据的 scope/capability/write root。
- **预防：** revision 必须记录 reason、authority impact、digest；Tool Gateway 校验当前 Plan Node。
- **检测：** before/after plan semantic scope diff 和 prohibited outcome match。
- **恢复：** 保留旧有效 plan，拒绝 revision，生成 overreach evidence。
- **fail-closed：** authority impact 未获批准时不激活新 plan。
- **测试：** 尝试添加 v10-G、外部项目写入、持续 Agent、merge/tag/push 节点，全部阻断。

### T07 — 模型请求秘密

- **风险：** secret、API key、cookie、credential 或私人标识进入模型、日志、截图或报告。
- **触发：** proposal 请求读取 secret 路径、环境变量、配置明文或全量数据库。
- **预防：** Provider Profile 只保存 `secret reference`；secret path/env denylist；最小上下文与 redaction。
- **检测：** 输入/输出/artifact/log secret scan，记录不含原值的 reason code。
- **恢复：** 停止调用、隔离含敏感值 artifact；如可能暴露则进入 `recovery_required` 并要求轮换。
- **fail-closed：** secret 命中时不发模型、不执行、不回显。
- **测试：** canary secret 覆盖文件、环境和工具输出，验证 Provider payload/receipt/report 均不含 canary。

### T08 — 路径逃逸

- **风险：** `..`、盘符切换、UNC、device path、ADS、case/prefix confusion 写出授权根。
- **触发：** typed path 规范化后不在 read/write root，或输入含危险路径语义。
- **预防：** Windows-aware canonical normalization；拒绝 UNC、device path、ADS、相对逃逸和不匹配盘符。
- **检测：** 记录原始路径、规范路径、root comparison 和拒绝 reason，不泄露无关绝对路径。
- **恢复：** 不触碰目标；清理仅限任务拥有的临时文件。
- **fail-closed：** 无法无歧义解析或边界相等性不成立即拒绝。
- **测试：** `..`、大小写、尾空格/点、相似前缀、`C:`、`\\server`、`\\?\`、`file:stream` 全矩阵。

### T09 — Symlink/junction/reparse point 逃逸

- **风险：** 表面位于授权根内的路径解析到根外。
- **触发：** 路径任一 component 是 symlink、junction 或其他 reparse point。
- **预防：** 逐 component 检查 reparse metadata；对 existing parent 和 final target 做 real-path containment。
- **检测：** execution 前后二次解析，记录 component digest/type。
- **恢复：** 拒绝操作；若执行期间路径被替换则标记 race/recovery_required。
- **fail-closed：** 无法验证 reparse 安全性时禁止读写。
- **测试：** 文件 symlink、目录 junction、父目录替换和 TOCTOU fixture 均不得逃逸。

### T10 — 任意 shell

- **风险：** 模型获得 PowerShell/cmd/bash 字符串执行能力，绕过所有 capability 控制。
- **触发：** proposal 指定 shell executable、`-Command`、脚本文本或未注册 command。
- **预防：** capability_id + typed args + 固定 argv template；`shell=false`；只允许 registry executor。
- **检测：** executable/template/argv 与 registry version 比对。
- **恢复：** 拒绝并写 high-risk overreach；不做等价隐式执行。
- **fail-closed：** 未登记 executable/template 一律不可执行。
- **测试：** PowerShell、cmd、bash、Python `-c`、脚本 stdin 等任意代码入口全部拒绝。

### T11 — 命令注入

- **风险：** 参数通过 `&&`、`||`、管道、重定向、命令替换或环境变量改变命令语义。
- **触发：** typed 参数含 shell metacharacter、控制字符、未授权 env 或交互选项。
- **预防：** argv 数组直接启动；参数 schema/枚举；环境 allowlist；stdin 关闭；timeout 固定。
- **检测：** resolved argv 再验证，拒绝交互/网络/重定向特征。
- **恢复：** 终止任务拥有的子进程并保存截断日志。
- **fail-closed：** schema 外参数或危险 token 直接拒绝。
- **测试：** `&&`、`||`、`|`、`>`、`<`、`$()`、反引号、`%VAR%`、换行注入全部阻断。

### T12 — 输出注入

- **风险：** stdout/stderr、HTML、Markdown 或 artifact 注入 UI、日志解析器或后续模型上下文。
- **触发：** 输出含脚本、控制序列、伪造 receipt、超大数据或结构边界突破。
- **预防：** UI 转义；结构化 envelope；长度上限；大输出落受控 artifact；去除终端控制码。
- **检测：** content-type/schema/size/encoding 校验和 UI CSP。
- **恢复：** 隔离原始 artifact，只展示安全摘要；重建受影响视图。
- **fail-closed：** 无法安全解析的输出不进入控制面或 verification。
- **测试：** XSS、ANSI、伪 JSON 边界、超大输出、Unicode bidi/control fixture。

### T13 — 重复工具调用

- **风险：** 相同节点被多次执行，导致重复写入、重复费用或状态分叉。
- **触发：** retry、恢复、Provider 重发或并发 consumer 使用相同 idempotency identity。
- **预防：** 复用 v8 exactly-once/receipt；tool execution 绑定 objective/plan/node/proposal/idempotency digest。
- **检测：** 唯一约束和 terminal receipt lookup；并发 lease 冲突。
- **恢复：** 返回已验证 receipt，不重新执行；digest 不同则冲突。
- **fail-closed：** 状态不确定时进入 `recovery_required`，绝不盲重试。
- **测试：** 串行、并发、crash-before/after-receipt 重放，确认副作用恰好一次。

### T14 — Replay

- **风险：** 已消费授权、旧 proposal 或旧 acceptance 被用于新目标/新计划。
- **触发：** nonce/one-time identity 已使用，或 digest/time/project 不匹配。
- **预防：** authority nonce、有效期、目标/计划 digest、消费状态和 append-only ledger。
- **检测：** replay lookup 与 sequence validation。
- **恢复：** 拒绝重放，要求新 authority；保留旧结果只读证据。
- **fail-closed：** 不能证明是允许的 idempotent replay 时均拒绝。
- **测试：** 相同、变更 payload、跨项目、跨计划、过期 acceptance replay。

### T15 — 授权过期

- **风险：** 长任务或服务恢复后继续使用失效权限。
- **触发：** 当前时间超过 `expires_at`，或恢复时 authority 已撤销/不完整。
- **预防：** 每个 proposal 执行前和 checkpoint 恢复时重新校验有效期。
- **检测：** supervision heartbeat 标记剩余有效期与 `authority_expired`。
- **恢复：** 安全停止，状态进入 `waiting_authority`；不恢复写节点。
- **fail-closed：** 已过期 authority 绝不执行或续期。
- **测试：** fake clock 覆盖执行前、执行中、重启后三种过期点。

### T16 — 预算失控

- **风险：** model calls、token、cost、步骤或持续时间超出用户授权。
- **触发：** 任一计数达到 max model calls/token/cost/steps/duration。
- **预防：** Authorization Artifact 固定硬预算；每次调用前预留、后结算；不自动切 Provider。
- **检测：** Runtime usage 与本地监督计数交叉核对。
- **恢复：** 停止新调用，保留 checkpoint，进入 `waiting_authority` 或 failed。
- **fail-closed：** usage 缺失/无法计量时不继续付费调用。
- **测试：** fake usage 超限、并发预留竞争、Provider 少报/缺报 usage。

### T17 — 无限循环

- **风险：** Agent 在 plan/propose/observe/replan 间无界循环。
- **触发：** 重复 observation/proposal digest、无进展 revision 或 max steps 接近上限。
- **预防：** max steps/model calls/retries；progress digest；同等重试最多一次。
- **检测：** repeated-state detector 和 no-progress counter。
- **恢复：** 终止 loop，保存 checkpoint/trace，标记 failed 或 waiting。
- **fail-closed：** 不能证明新进展则不继续循环。
- **测试：** Fake Provider 永远返回相同 proposal，验证有限步退出。

### T18 — 卡死

- **风险：** 进程 20 分钟无实质进展，占用资源或阻塞任务。
- **触发：** PID 存活但 CPU、日志 mtime、artifact growth、步骤均无进展，或等待交互/锁/网络。
- **预防：** capability timeout、非交互执行、heartbeat、20 分钟 stall check（测试用缩短时钟）。
- **检测：** 检查 PID/CPU/memory/stdout/stderr/log mtime/output growth/network/SQLite lock/children/last step。
- **恢复：** 标记 `suspected_stall`，保存日志，优雅终止并清理 owned children；调整后最多一次等价重试。
- **fail-closed：** 不无限等待、不无限重跑、不把卡死标为成功。
- **测试：** 确定性冻结进程和锁等待 fixture，验证检测、终止、证据和单次重试。

### T19 — 进程残留

- **风险：** 取消、失败或崩溃后子进程继续运行并持有文件/端口/权限。
- **触发：** parent 退出、timeout、cancel 后 owned process tree 仍存在。
- **预防：** process ownership token/job object；记录 PID/children；禁止 detached daemon。
- **检测：** terminal cleanup 后枚举 owned process tree、端口和锁。
- **恢复：** 先优雅终止，超时后仅终止 owned tree；记录残留处置。
- **fail-closed：** 无法证明归属不终止外部进程，并进入 `recovery_required`。
- **测试：** child/grandchild、端口监听和 DB lock 残留场景。

### T20 — 网络外传

- **风险：** 仓库、私人状态、secret 或完整数据库被发送到未授权网络。
- **触发：** capability/provider 请求未知 endpoint、重定向、下载或超出上下文预算的数据。
- **预防：** `network=false` 默认；Provider endpoint allowlist；禁止工具网络；最小上下文和 secret filter。
- **检测：** 请求 destination、payload digest/size/source manifest 审计，不记录敏感正文。
- **恢复：** 中止调用、隔离 artifact；疑似泄漏进入安全事件流程。
- **fail-closed：** endpoint/redirect/TLS/permission 不符合即不发送。
- **测试：** 未授权域名、HTTP redirect、DNS/loopback 混淆、工具隐式联网全部拒绝。

### T21 — Provider 故障

- **风险：** timeout、错误响应、限流或服务不可用造成错误状态或隐形模型切换。
- **触发：** health failure、transport error、non-2xx、timeout、malformed payload。
- **预防：** Provider Registry health state、timeout、明确手选；不自动换 Provider。
- **检测：** v8 Runtime 写 sanitized failure/usage/receipt，Provider health 降级。
- **恢复：** 同 Provider 且授权内最多按策略重试；否则等待用户选择。
- **fail-closed：** 不伪造回答、不暗中切换、不执行不完整 proposal。
- **测试：** timeout、429、5xx、断连、部分响应，确认权限/状态不变。

### T22 — 错误结构化输出

- **风险：** 缺字段、未知 capability、schema confusion 或类型绕过导致错误执行。
- **触发：** Provider JSON 非法、字段额外/缺失、数值溢出、重复 key。
- **预防：** strict JSON Schema、版本绑定、canonical parse、拒绝 unknown fields/capability。
- **检测：** parser/schema errors 写 sanitized failure artifact。
- **恢复：** 允许同预算的一次受约束修复请求；仍失败则停止。
- **fail-closed：** 未通过 schema 的内容只能作为文本，不得成为 Tool Proposal。
- **测试：** malformed JSON、duplicate keys、NaN、wrong type、unknown version/capability。

### T23 — 上下文过量

- **风险：** 发送全量数据库、无关私人状态或超出 token/privacy 预算。
- **触发：** context assembler 超过 source/item/token/byte 限制或来源不必要。
- **预防：** Objective 驱动的最小必要选择、per-source budget、摘要和 source anchor。
- **检测：** context manifest 记录来源、选择原因、大小、token estimate 和 exclusions。
- **恢复：** 缩减/重新选择上下文，不自动扩大预算。
- **fail-closed：** 不能满足预算或来源边界时不调用 Provider。
- **测试：** 巨型 memory/tool output、全库请求、跨项目私人内容，验证裁剪与阻断。

### T24 — 过期状态

- **风险：** Agent 基于旧 Now/Resume Package 作出错误计划或执行。
- **触发：** freshness TTL 过期、source observation 版本落后、项目 digest 改变。
- **预防：** 复用 v9 freshness；执行前重取关键 read-only observation。
- **检测：** context binding 中保存 observed_at/freshness/source digest。
- **恢复：** 计划重验证并生成 Plan Revision；authority impact 变化则重新授权。
- **fail-closed：** 高影响事实过期时不执行。
- **测试：** fake clock、项目文件变化和数据库 generation 变化。

### T25 — 缓存状态误导

- **风险：** 离线/PWA/浏览器缓存被当作实时生产状态或授权依据。
- **触发：** cached response、offline snapshot 或 Service Worker 数据参与执行判断。
- **预防：** 私人 API `Cache-Control: no-store`；缓存明确标注 cached/offline；执行控制面不读前端缓存。
- **检测：** source mode、age、network status 可见；比较实时 authority store。
- **恢复：** 回到 live local authoritative read；离线时只读展示。
- **fail-closed：** cached content 不能授权写入、接受结果或更新完成状态。
- **测试：** 离线 stale Objective/authority/verification，确认移动端无写且执行被阻断。

### T26 — 把 Runtime success 当作 Acceptance

- **风险：** Provider/工具成功后项目被误标为用户已接受完成。
- **触发：** terminal receipt 直接驱动 accepted/completed projection。
- **预防：** 四态分离；Acceptance Policy 与 Acceptance Event 独立；Agent 不是 accepter。
- **检测：** projection invariant 要求 accepted 状态必须引用有效 acceptance event。
- **恢复：** 重建 projection，撤销错误 accepted 显示并保留实际 execution/outcome。
- **fail-closed：** 缺 acceptance event 只能进入 `waiting_acceptance`。
- **测试：** Agent 自报成功、tool exit 0、verification pass 三种情况下仍不得自动接受，除非预授权确定性低风险 policy 满足。

### T27 — 误更新长期画像

- **风险：** 一次行为/失败被写成稳定偏好或永久规则。
- **触发：** Outcome 直接写入 v7 long-term profile/memory。
- **预防：** Outcome → Fact → Observation → Pattern/Workflow Candidate 分层；批准或多次证据门禁。
- **检测：** 写路径 policy 禁止 cognitive run 直接更新长期画像表。
- **恢复：** 拒绝写入并生成 learning candidate；若误写则进入审计恢复。
- **fail-closed：** 证据/批准不足只保存 candidate。
- **测试：** 单次成功/失败/突破 stance 均不能改变稳定偏好。

### T28 — 移动端越权

- **风险：** 移动端创建任务、授权、接受高风险结果或调用写工具。
- **触发：** mobile/Tailnet 客户端请求 mutation/admin/act endpoint。
- **预防：** mobile surface GET-only；无写控件；服务端路由级拒绝，而非仅隐藏按钮。
- **检测：** HTTP method matrix、route inventory、mobile browser network capture。
- **恢复：** 返回拒绝且不产生 authority/execution；记录安全事件。
- **fail-closed：** 未能确认 desktop local trust 时按移动只读处理。
- **测试：** POST/PUT/PATCH/DELETE、伪造桌面 User-Agent、直接 API 请求全部拒绝。

### T29 — Tailnet 写入

- **风险：** Tailscale Serve 转发到 loopback 后被误判为本机，绕过 admin 边界。
- **触发：** 反向代理请求访问 Agency Admin Service 或 mutation route。
- **预防：** 写服务独立端口/进程、默认关闭、不可被 Serve 路由；短期 capability + 独立 session/CSRF；
  不仅依赖 remote IP/Origin/User-Agent。
- **检测：** Serve config 审计、端口可达性和 Tailnet 真实负向请求。
- **恢复：** 立即关闭 admin service/capability，撤销 session，审计已执行 receipt。
- **fail-closed：** 无法证明 Tailnet 不可达时采用 local CLI/local broker，不开放 Web 写入。
- **测试：** 通过实际 Serve 路径探测所有 write method/admin route，必须不可达或拒绝。

### T30 — CSRF

- **风险：** 本机浏览器被外部页面诱导向 local admin 发起写请求。
- **触发：** 跨站表单/fetch、旧 session、缺 token 或 token 重放。
- **预防：** 独立 session、短期 single-task capability、CSRF token、SameSite、Origin/Host 校验和非简单 content-type；
  Origin 仅为多重控制之一。
- **检测：** token/session/task binding 与 replay 记录。
- **恢复：** 拒绝请求，撤销任务 session/capability。
- **fail-closed：** 任一绑定缺失或不一致即拒绝。
- **测试：** 跨 Origin 表单、缺/错 token、过期 token、重放、Host confusion。

### T31 — Schema drift

- **风险：** Operational DB 实际 schema 与 migration/marker 不一致，执行到未知结构。
- **触发：** 未知 table/index/trigger、marker/digest/user_version 不匹配。
- **预防：** additive migration 固定 digest；managed schema fingerprint；执行前后全量比对。
- **检测：** schema inventory、marker、digest、`integrity_check`、`foreign_key_check`。
- **恢复：** 停止 writer，使用 verified backup 在独立路径诊断/恢复。
- **fail-closed：** 未知 drift 不自动修复、不自动重试，标记 `recovery_required`。
- **测试：** 增删改 schema object、相同 version 不同 digest、缺 marker、高版本。

### T32 — Projection corruption

- **风险：** event ledger 正确但 current projection/Now/Resume/accepted 状态错误。
- **触发：** projection digest、sequence、source event head 或 snapshot 不一致。
- **预防：** append-only event 为权威；projection 可从 genesis/verified snapshot 确定性重建。
- **检测：** event-chain、snapshot、projection digest 和业务 invariant 比对。
- **恢复：** 在克隆验证 rebuild 后，受控重建 projection；不修改事件历史。
- **fail-closed：** corruption 时停止 act/acceptance，读界面显示 unverifiable/recovery_required。
- **测试：** 篡改单行 projection、丢 sequence、坏 snapshot，证明检测和确定性重建。

### T33 — Backup failure

- **风险：** 迁移前没有可恢复备份，或 package 缺库/sidecar/assets/manifest。
- **触发：** 空间不足、copy/online backup 失败、hash/逻辑摘要不一致。
- **预防：** 五库 + DB/WAL/SHM gates + assets 的 release backup；manifest 和 backup-set digest；
  独立 restore 验证后才允许生产窗口。
- **检测：** 文件 size/SHA-256、SQLite integrity/FK/schema/logical digest、asset tree audit。
- **恢复：** 丢弃失败 package，保持生产只读；解决原因后重新生成。
- **fail-closed：** 未验证 backup 不得进入 production migration。
- **测试：** 磁盘不足、截断 DB、缺 assets、hash mismatch、句柄/锁失败。

### T34 — Restore mismatch

- **风险：** restore 后五库、assets、Objective/Plan/Run、Now/Resume 或搜索结果不一致。
- **触发：** package 损坏、错误源、sidecar 污染、恢复连到生产路径。
- **预防：** 独立路径/端口 restore；连接路径断言；manifest/digest/逻辑查询双重验证。
- **检测：** 五库与 asset tree equality，关键领域摘要和只读 UI/API 对照。
- **恢复：** 删除仅属于演练的 restore 实例，保留生产不变；选择已验证 package 重试。
- **fail-closed：** 任一 mismatch 则 package/迁移均 NO-GO。
- **测试：** 换库、篡改 manifest、缺 sidecar、错误端口/路径、Objective/Plan/Run 差异。

### T35 — Agent 自己创建下一版本

- **风险：** Agent 将延期问题升级为 v10-G、v10.1、v11 或修改根目标。
- **触发：** Objective/Plan/Tool Proposal/文件名/分支/tag 中出现未授权下一版本。
- **预防：** prohibited outcomes + `next-version creation=false`；capability/Git/file policy 拒绝保留版本之外的节点。
- **检测：** version token、branch/tag target、plan scope 检查，记录 overreach。
- **恢复：** 丢弃 proposal/revision；保持当前有效 plan 和 v10-F 停止点。
- **fail-closed：** 不创建文件、分支、commit、tag 或任务状态。
- **测试：** 直接和间接请求 v10-G、G0、v10.1、v11，全部拒绝且 v9/v10 状态不变。

## 5. 组合攻击与独立验证

单项通过不足以证明安全。v10-D/v10-F 必须至少覆盖以下组合：

- prompt injection + 历史伪授权 + tool output injection；
- Tailnet loopback confusion + CSRF + 伪造 desktop User-Agent；
- path traversal + junction + TOCTOU；
- duplicate call + crash + expired authority + recovery；
- Provider malformed output + hidden switch suggestion + budget exhaustion；
- tool exit 0 + forged Agent report + missing verification + self-acceptance；
- schema drift + corrupt projection + backup/restore mismatch；
- stalled child process + cancellation + residual process cleanup；
- cached stale state + offline mobile mutation attempt；
- successful task + learning candidate，证明长期画像不变。

每项测试必须记录 Objective/Plan/Authority digest、expected policy decision、实际 reason code、
execution/receipt（若存在）、verification、acceptance state、数据库/文件不变量和残留进程。

## 6. 安全 Gate

只有以下全部成立，v10 安全节点才可为 `GO`：

1. T01–T35 均有自动化或明确人工验收证据；
2. 所有越权场景 fail-closed，生产项目完成状态不更新；
3. Search 模式无 Provider execution/usage/费用；
4. 模型不能伪造 authority、tool result、verification 或 acceptance；
5. Tailnet/mobile 不能到达写入能力；
6. secret canary 不进入 Provider payload、日志、artifact、截图或发布报告；
7. recovery 不重复成功工具调用，过期 authority 不恢复；
8. Agent 无法 merge/tag/push、修改长期画像或创建下一版本；
9. 全量测试最终 `failed=0`、`errors=0`，环境型 skip 逐项说明。

任一 Gate 缺少证据即 `NO-GO`，不得以 Agent 自检 GO 替代系统验证或用户接受。
