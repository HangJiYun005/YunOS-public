# YunOS v10-D — 监督与恢复

## 1. 目标

v10-D 将 `Cognitive Run`、模型调用、工具调用和独立验证持久化为可审计状态机。监督层的职责是
发现无进展、越权、预算耗尽、授权过期和未知执行结果；恢复层只允许从最后一个可证明安全的
checkpoint 继续。两者都不签发新权限，也不把工具成功升级成用户接受。

## 2. 权威状态与持久化

监督与恢复复用 v9 Operational State DB：

- `cognitive_runs` 保存状态、预算使用和最后活动；
- `cognitive_run_checkpoints` 保存 Plan 进度、当前节点、已完成节点、model budget state、最后 tool
  execution 与 `safe_to_resume`；
- `tool_proposals` 保存模型建议，`tool_executions` 只保存 immutable terminal receipt；
- v9 `state_events` 保存模型预留/结算、sealed tool preview、工具预留和 supervision event；
- v9 `state_verifications` 保存独立验证；
- v9 writer lease 提供单写者 CAS 边界。

模型调用的 execution/receipt/usage/artifact/ledger 仍在 v8 Runtime DB。Operational State 只保存
稳定 reference/digest。没有创建第六数据库，也没有第二套 receipt 或 ledger。

## 3. Run 状态机

实现支持 `queued`、`reasoning`、`planning`、`waiting_authority`、`executing`、
`waiting_external`、`verifying`、`waiting_acceptance`、`completed`、`failed`、`cancelled` 和
`recovery_required`。

关键门禁：

- `executing` 必须有未过期且与 run/plan/project 完整匹配的 Authorization；
- 存在未决 model/tool reservation 时，不能前进到 `verifying`、`waiting_acceptance` 或
  `completed`；
- 将未决 reservation 关闭为 `failed`/`cancelled` 会规范化为 `recovery_required`，不能假装安全；
- tool terminal success 只进入 `verifying`，不会完成 Plan node；
- 独立 verification 为 `passed` 后才能完成节点并回到 `executing`；
- 所有必需节点完成且 checkpoint 安全后最多进入 `waiting_acceptance`；
- `completed` 必须引用同 project、delegation、run 和 verification 的真实 user acceptance event。

状态更新使用 expected run digest、writer lease 和 checkpoint sequence 做 CAS。canonical row、
denormalized projection、run counters 或 model budget state 任一漂移都会 fail-closed。

## 4. Exactly-once 与预留顺序

### 4.1 模型调用

在 v8 Runtime send 前，Operational State 先写入 `ModelCallReservation` 和
`model_call_reserved` event，并增加保守预算：

- calls、input tokens、output tokens 和 cost ceiling 先预留；
- request/provider profile/version/digest 与 authorization 精确绑定；
- 同一未决 reservation 重启后返回 `recovery_required`，绝不再次发送；
- settlement 必须绑定 v8 execution reference、receipt identity/digest、usage digest 和实际 usage；
- Provider cost 未知时保留完整 unknown cost ceiling，不按零成本结算；
- 相同 receipt 与相同 usage 可幂等重放，不同 usage 触发 collision；
- outcome 未知时进入 `model_call_recovery_required`。

Unknown outcome 不能由原 Cognitive Authorization 自行“结算”。系统只能为一个既有 reservation
创建 factory-sealed、lease-bound 的 `SystemModelReconciliationAuthority`，将其隔离并保留完整
input/output/cost reservation ceiling；该权限不能再次调用 Provider，也不能生成 Tool Proposal。
若 Runtime 已有可信 terminal receipt/usage，则必须先按 `succeeded` 或 `failed` 结算，再处理
structured output，不能因为解析错误把已知调用误标为 unknown。

### 4.2 工具调用

Tool Gateway 在效果发生前持久化 sealed preview 和 reservation。Preview 固定绑定 proposal、Plan
node、Authorization、attempt、path identity、command template、resolved argv、timeout、输出上限和
digest。效果之后只允许写入与该 reservation 完全一致的 terminal result。

未决 reservation 不允许普通代码重试。只有在读取到可证明的 durable observation 后，才能通过
受限 `SystemReconciliationAuthority` 记录 terminal；否则保留未决记录并标记
`recovery_required`。

## 5. 20 分钟卡死协议

`ProcessSupervisor` 的生产默认 `DEFAULT_STALL_CHECK_MS = 20 * 60 * 1000`。它以 `shell=false`、
`stdin=DEVNULL` 启动一个 code-owned argv，并持续采集：

- PID、start/finish time、duration 和 exit code；
- Windows 可获取时的 CPU seconds 与 memory bytes；
- stdout/stderr 增长、artifact 增长和最后进展；
- input wait 固定为 `stdin_disabled`；
- timeout、output truncation、secret redaction 和 child cleanup 结果。

一个检查周期内无可证明进展会生成 `suspected_stall`，保存已有限量日志，先尝试 graceful
termination，再清理由本任务拥有的进程树；若仍残留则 fail-closed。等价 retry 最多一次，且必须
创建新 attempt、保留旧 reservation/receipt，并且仅在前次独立证明无 owned change 时允许。

测试不真实等待 20 分钟：把 stall clock 缩短为 40 ms，运行安全的 sleep fixture，断言
`suspected_stall=true`、存在 observation、owned PID 已退出且 `residual_process=false`。

## 6. 中断与恢复

恢复入口按以下规则处理：

1. 读取最新 run/checkpoint，并核对 canonical digest、sequence、model/tool inventory 和 counters；
2. 已结算 model receipt 或已记录 tool terminal 不重复执行；
3. 未决 model reservation 不重发；只允许与原 reservation 绑定的 reconciliation；
4. 未决 tool reservation 必须有 durable runtime receipt、local broker backup 或其他可独立验证的
   observation；否则保持 `recovery_required`；
5. 授权过期时拒绝所有新执行；只允许 lease-bound 的 reconciliation 关闭旧不确定状态；
6. 需要人工确认的 run 保持 `waiting_authority` / `waiting_acceptance`；
7. 独立验证记录丢失或不可验证时不完成节点；
8. rollback 只接受当前 run 的 backup reference 与当前文件 SHA-256，不触碰其他变更。

用户取消是 sealed artifact，而不是模型文本。安全 checkpoint 可直接进入 `cancelled`；若取消发生
在未决 model/tool/verification 上，原 unsafe reason 会被保留并追加 cancellation marker，恢复器先
做 reservation-specific reconciliation，再返回取消结论。`waiting_external` 只有在持久链证明前次
执行 failed、独立验证为 failed 且 `tool_execution_no_owned_change` 时，才可用于一次等价 retry 或
一次同节点 bounded replan；未知外部等待始终不可恢复执行。

恢复成功不代表用户接受；其结果仍须经过 Verification 与 Acceptance Policy。

## 7. Supervision Event

支持的事件包括 `heartbeat`、`checkpoint`、`suspected_stall`、`stall_cleared`、
`budget_warning`、`budget_exhausted`、`overreach_blocked`、`authorization_expired`、
`provider_failure`、`process_terminated`、`retry_started`、`recovery_started`、
`recovery_completed` 和 `cancellation_requested`。

事件按 run、sequence、time、severity、plan node/tool execution、evidence digest 和 action taken
持久化。相同语义事件可幂等读取；不同 payload 复用 identity 会 collision。日志和 evidence 不保存
secret 原值。

## 8. 验证证据

本次冻结树 `test_v10_*` 统一复验覆盖 444 tests，444 passed，0 failed，0 errors，0 skipped，
441.878s。其中恢复相关
证据包括：

- 未决 tool reservation 阻止所有前进状态；
- `failed`/`cancelled` 在不确定效果下收敛为 `recovery_required`；
- expired authority 拒绝新执行，只允许精确 reconciliation；
- executor self-check 不能完成 Plan node；
- 独立 verification passed 才解锁节点；failed/unverifiable 均 fail-closed；
- 第二 attempt 仅在独立证明前次无 owned change 后允许；
- model reservation 跨重启持久化且不二次发送；
- 相同 receipt settlement 幂等，不同 usage collision；
- 短时钟 stall 终止 owned process 且无残留；
- patch backup、完整 rollback 和 Windows-safe artifact name 通过。

完整命令见 [受限执行](./bounded_execution_v10_d.md#8-真实沙箱验收)。统一日志 SHA-256 为
`0000000000000000000000000000000000000000000000000000000000000000`；整个仓库最终全量为
1,942 collected / 1,936 passed / 6 environment skips / 0 failed / 0 errors / 668.747s，日志 SHA-256
为 `0000000000000000000000000000000000000000000000000000000000000000`。

## 9. 已知限制

- 本节点的 restart 是进程内重建 store/loop 并读取同一临时 SQLite 的确定性演练；v10-F 仍需在
  独立服务进程和恢复包环境完成正式中断恢复；
- `ProcessSupervisor` 只清理它直接拥有的进程树，不得清理系统或其他任务进程；
- 网络等待、SQLite lock 和外部 Agent 进度需要具体 executor 提供可验证 observation；未知时直接
  `recovery_required`；
- production schema migration、五库备份/restore、writer-off/real-readonly 和 Tailnet 复验属于
  v10-F；
- desktop/mobile 的卡死、恢复和等待接受可视化属于 v10-E。
