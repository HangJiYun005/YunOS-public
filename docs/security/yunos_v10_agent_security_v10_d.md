# YunOS v10-D — Agent Security 实现关闭

## 1. 文档定位

本文记录 v10-D 已落地的 Agent 安全控制和定向证据，是
[v10 Threat Model](./yunos_v10_threat_model.md) 的实现映射，不复制或修改其中 35 项威胁定义。
本文不授权生产 migration、Web/Tailnet 写入、Provider secret 变更、`merge`、`tag` 或 `push`。

## 2. 信任模型

以下输入全部不可信：用户自由文本、历史聊天、Memory Package 内容、Provider 输出、工具 stdout/
stderr、Codex handoff 返回和 executor 自报验证。它们可以形成候选信息，但不能形成 Objective、
Plan、Authorization、Tool receipt、System verification 或 User acceptance 的权威事实。

安全控制由代码持有：

- v7 提供带来源的 Memory/Evidence，不授予执行权；
- v8 Runtime 提供模型 execution/receipt/usage/artifact/ledger，不签发权限或接受结果；
- v9 Operational State 提供 Plan/Run/state event/writer lease/Verification/Acceptance 权威状态；
- v10 Tool Gateway 是模型 proposal 与真实效果之间唯一执行边界；
- Provider selection、Agency Level 和 Cognitive Stance 都不能扩大 Authorization。

该设计只使用现有五库。v10 的执行状态使用 Operational State schema `10001`，不创建第六库。

## 3. 安全不变量与实现证据

| 不变量 | v10-D 控制 | 失败行为 |
| --- | --- | --- |
| 模型不能 mint authority | factory-only Authorization；绑定 user source、Objective/Plan/Delegation digest | proposal 被拒绝，不产生 reservation |
| Proposal 不能扩大 Plan | proposal 必须绑定已持久化可执行 node、依赖、risk 与 capability | `agent_overreach_blocked` 或 scope reason code |
| 先记录再产生效果 | model/tool reservation 在 send/write/process 前原子持久化 | 未决状态进入 `recovery_required`，不盲重试 |
| 无任意 shell | closed typed args、code-owned template、fixed argv、`shell=false`、stdin disabled | shell token/template drift 被拒绝 |
| 无根外文件访问 | Windows canonical path、root identity、reparse/hardlink/ADS/UNC/device 检查 | preview 前或 execute 重验时拒绝 |
| 无 secret 外传 | context/input/output/artifact secret filter；secret reference 与值分离 | 整个 item/output 省略、redact 或 fail-closed |
| Provider 不是安全边界 | 结构化输出仅生成 proposal；Gateway 重做全部授权检查 | 模型声称成功/授权均无效 |
| Tool success 不是 Verification | 独立重读/hash/query/Git/process artifact | Plan node 保持未完成或 `recovery_required` |
| Verification 不是 Acceptance | verifier 固定 `grants_acceptance=false`；Agent outcome 固定 `user_accepted=false` | 最多 `waiting_acceptance` |
| Agent 不能创建下一版本 | semantic guard + prohibited outcome/authority boundary | `v10-G`/`v10.1`/`v11` 意图在执行前阻断 |
| Agent 不能 merge/tag/push/network/DB write | 默认 denial、无对应 executable gateway path、语义 overreach guard | fail-closed 并记录 supervision |
| 重启不能重复调用 | reservation inventory、receipt/usage binding、checkpoint CAS | 不重发模型、不重复工具效果 |
| 预算不能失控 | calls/token/cost 预留；unknown price 保留 ceiling | 超预算拒绝，未知 settlement 转恢复 |
| 已知模型结果不能被误隔离 | terminal receipt/usage 先 settlement，structured parse 后置 | 解析失败不改写 Runtime 真值 |
| 取消不能抹除未决风险 | sealed user cancellation 保留原 unsafe reasons，先 reconciliation | 保持 `recovery_required`，不跳过未决效果 |
| 卡死不能无限运行 | 20 分钟默认 stall check、owned process termination、最多一次等价 retry | `suspected_stall` / `recovery_required` |

## 4. Prompt 与工具输出注入

模型只接收带 source/digest/budget 的最小上下文。上下文中的指令文本不参与 Authorization
计算；Provider 输出也只能通过 closed structured output 解析。Agent Loop 在创建正式 proposal 前
还会阻断下一版本、shell、network、database write、merge、tag 和 push 能力。

Tool Gateway 不相信模型提供的“已验证”“用户已授权”或 path 字符串：所有 Plan、authority、risk、
path、command 和 budget 均从权威对象重新计算。工具输出发现 secret material 时不会继续作为模型
observation 或验证证据。

## 5. 路径与本地效果隔离

路径分两阶段验证：preview 解析并封存 identity，execute 紧邻效果前重验。Windows 特有攻击面
包括 UNC、`\\?\` device path、drive switching、ADS、reserved device names、尾随点/空格、case
prefix confusion、symlink/junction/reparse point、nested `.git`、hardlink 和 temp ownership。

写工具仅支持精确文件 patch/create/rollback。Patch 必须匹配 before SHA-256，create 使用 exclusive
create，rollback 必须匹配当前 SHA-256 和本 run backup record。Backup 文件名使用 canonical
SHA-256 storage key，避免 Windows 中 identity 的冒号成为非法路径。

数据库检查只使用 SQLite `mode=ro` + `query_only`，在检查前后封存主库与 WAL/SHM sidecar
snapshot，并运行 integrity/FK 观察；不存在任意 SQL 写能力，也不使用会忽略 WAL 的
`immutable=1`。

## 6. 命令、进程与日志

模型不能提供可执行路径、完整 argv、shell 字符串或环境变量。`CommandTemplateRegistry` 从 typed
arguments 解析固定模板，记录 executable device/inode/size/SHA-256 和 template digest。execute 时任何漂移
都会拒绝。

Supervisor 限制 timeout、stdout/stderr 和 artifact size，记录 PID、CPU、memory、last progress、
digest 和 termination 结果。默认每 20 分钟检查无进展；测试使用可注入短时钟，不改变生产默认值。
检测 stall 后只终止 owned process tree，不能清理任意 PID。secret output 在 result 与两个 artifact
路径上均被过滤。

## 7. Replay、恢复与伪造结果

Model reservation 和 Tool reservation 都先于外部效果写入 v9 `state_events`。已结算 receipt 的
相同重放返回原结论；同 identity 不同 payload 产生 collision。未决 reservation 跨重启保留，普通
路径不允许猜测或重复执行。

Provider price 为 unknown 时，production turn source 使用 Registry 真值并预约有效完整 cost
ceiling；caller 不能传入更低的估算值。Provider/Plan node/Authorization 被重新封进受预算 context
digest，调用方 context 不能省略当前节点或暗中切换 Provider。一次 replan 只携带独立验证 evidence
digest 与固定 data-only 原因，不携带工具输出全文，也不获得新权限。

`SystemReconciliationAuthority` 只在存在原 reservation 和当前 writer lease 时创建；它最多关闭
一次旧 reservation，不能开始新调用。没有 durable observation 时保持 `recovery_required`。

取消请求必须由 factory-sealed `UserCancellationArtifact` 提供，并绑定 run、user、session 与
CSRF digest。取消与未决 model/tool/verification 同时存在时，恢复入口先隔离或清算该 reservation，
保留原 unsafe reason，再返回取消状态；模型文本或工具输出不能伪造取消。

executor 的 terminal result 绑定 proposal/preview/authorization/attempt，但仍不是 verification。
独立 verifier 重新观察目标并把证据写入 v9 `state_verifications`；executor 自报 success 或伪造
`verification_passed=true` 都不能完成 Plan node。

## 8. Acceptance 与长期学习边界

Agent Loop 的结果类型强制 `user_accepted=false`，并拒绝返回 `completed`。只有 v9 权威
acceptance event 与 project、delegation、run、verification 全部匹配时，状态才可进入 completed。

v10-D 不直接修改 v7 长期画像。Tool outcome 只可成为 Fact/Observation/Workflow Candidate；长期
偏好仍需用户批准或多次证据。Cognitive Stance 影响方案选择，不改变权限。

## 9. Codex 边界

本机 Codex 桌面包资源可见，但当前 shell token 不能执行包内二进制的 `--help` 或 `--version`，
因此未证明可靠的受支持 CLI/Agent 接口。Adapter 固定为 `handoff_only`，任何 execute 都
fail-closed；没有 UI 自动点击、ACL 绕过、二进制复制、逆向工程或虚构成功。

Handoff artifact 是不可信转交材料，不携带 secret、authority、execution success 或 acceptance。
详见 [Codex Adapter v10-D](../codex_adapter_v10_d.md)。

## 10. 定向安全证据

本次冻结树 `test_v10_*` 统一复验为 444/444 passed，0 failed，0 errors，0 skipped，
441.878s，覆盖：

- Authorization factory seal、expiry、cross-run/project/digest binding；
- malicious Windows path matrix、reserved names、prefix/reparse/identity drift，以及真实 junction 下
  read/list/search/SQLite parent-swap TOCTOU；
- template mismatch、shell injection、executable drift；
- proposal/preview/result closed schema 与 tamper；
- model/tool reservation replay、unknown result、expired reconciliation、settled proposal restart 复用；
- executor forged verification、artifact tamper、write hash mismatch；
- out-of-scope write、next-version、shell/network/DB write/merge/tag/push overreach；
- short-clock stall、bounded stream queue、artifact root swap、secret redaction、owned-process cleanup；
- Agent 最多进入 `waiting_acceptance`，不能 self-accept。

节点全量已完成：1,942 collected / 1,936 passed / 6 environment skips / 0 failed /
0 errors / 668.747s。生产 Tailnet/mobile 写边界与 production migration fault injection 仍由
v10-E/F 继续验证。既有威胁定义和测试矩阵保持在
[v10 Threat Model](./yunos_v10_threat_model.md)，本文不重复展开 35 项逐条描述。

## 11. 已知限制

- v10-D 原生 Agent 测试使用 deterministic fake Provider/Runtime bridge；真实 Provider 与原生执行的
  组合验收留待 v10-F；
- production Operational State schema `10001` 未迁移，生产写通道未启用；
- Codex 为 handoff-only，未完成真实 Codex delegation；
- local admin service、CSRF/session、Tailnet 反向代理隔离和 mobile GET-only 的动态验收属于 v10-E/F；
- 最终全量、五库备份/restore、main/tag 均未在本节点文档中宣称完成。
