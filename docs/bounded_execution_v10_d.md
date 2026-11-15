# YunOS v10-D — 受限执行

## 1. 结论与边界

v10-D 已实现 YunOS 原生、有限、可恢复的受控执行链：模型只能提交结构化
`Tool Proposal`，实际能力必须依次经过 `Objective`、`Plan Graph`、
`Authorization Artifact`、`Capability Registry`、`Tool Gateway`、持久化预留、
执行回执和独立验证。Agent 只能把满足条件的 run 推进到 `waiting_acceptance`，不能接受
自己的结果，也不能创建持续后台任务。

本节点不开放生产 Web 写入口，不迁移生产 Operational State DB，不执行 `merge`、`tag`、
`push`，也不声明生产接纳。v10-D 的真实写验收仅发生在测试创建的临时沙箱目录内。

## 2. 复用关系

v10-D 没有建立第二套 Runtime、第二套状态系统或第六个数据库：

- 模型调用继续复用 v8 Runtime 的 execution、receipt、usage、artifact、ledger 和
  failure state；
- Objective、Plan、Authorization、Cognitive Run、checkpoint、tool proposal 和 tool
  execution 使用既有 Operational State DB 的 schema `10001` 加法式对象；
- 执行预留、模型预留、恢复和监督通过 v9 `state_events` 持久化；
- 独立验证复用 v9 `state_verifications`；Delegation、Outcome、Acceptance 和 writer lease
  均复用 v9 权威实体；
- 跨 v8 Runtime 与 v9 Operational State 的关系只保存稳定 identity、digest 和 receipt
  引用，不建立跨库 Foreign Key。

生产 schema `10001` migration 仍留在 v10-F。当前节点只在临时克隆数据库中执行该 schema
并验证约束。

## 3. 执行链

原生执行控制器位于 `memory/cognitive_agent_loop.py`，单次调用按以下顺序运行：

1. 读取已持久化 `Cognitive Run`、当前 checkpoint、Plan 和 Authorization；
2. 验证 Agency Level、project、objective/plan/authority digest、授权有效期和预算；
3. 在允许 Provider send 前持久化 `ModelCallReservation`；
4. production turn source 把当前 Plan node、能力需求、read/write scope 与禁止权限重新封入
   受预算 context；Provider 仍由用户手动选择；
5. 通过 v8 Runtime Bridge 发送一次模型请求，无隐藏 fallback 或自动重试；
6. 已知 terminal receipt/usage 先结算；只有缺少可信 terminal observation 的调用才由
   system-only reconciliation authority 隔离为 unknown，绝不重发；
7. 将模型输出当作不可信结构化数据解析为 `Tool Proposal`；
8. 对下一版本创建、任意 shell、网络、数据库写、merge、tag 和 push 做语义越权拦截；
9. `Tool Gateway` 重新验证 Plan node、Capability、Authorization、路径、命令和输出预算；
10. 在任何外部效果前持久化 `ToolExecutionPreview` 与执行预留；
11. 由本地 broker 或 code-owned 固定 argv 模板执行；
12. 持久化 terminal result，但不据此完成 Plan node；
13. 独立 verifier 重新观察文件、目录、SQLite，或重新验证 Git/process 的固定命令与受控
    artifact；
14. 只有 `state_verifications` 中的独立 `passed` 记录才完成节点；
15. 所有必需节点通过后进入 `waiting_acceptance`，不会自动进入 `completed`。

循环有明确的 `maximum_steps`、`maximum_duration_ms` 和 `maximum_model_calls` 上限，不创建
线程、调度器或常驻后台 Agent。服务重启后只从持久化 checkpoint 继续。

模型价格未知时，reservation 保守占用当前 Authorization 与 source limits 的有效完整 cost
ceiling；有效 ceiling 为零则在 Runtime send 前拒绝。价格已知时只使用 Provider Registry 的
cost metadata 按 input/output token ceiling 分项向上取整，调用方不能自行低报成本。

## 4. Authorization Artifact

`CognitiveAuthorizationArtifact` 只能由受控 factory 创建，并精确绑定：

- user-issued source authority、Objective/Plan/Delegation digest 和 project；
- read roots、write roots、allowed/denied capabilities 和 command template；
- risk ceiling、最大步骤、时长、模型调用、token 和 cost；
- commit、merge、tag、push、network、database write、next-version creation 和 persistent
  background agent 布尔边界；
- `valid_from`、`expires_at`、rollback policy 与 acceptance policy。

模型、Provider 输出、工具输出和反序列化后的任意对象都不能签发或扩大 Authorization。
默认禁止 `merge`、`tag`、`push`、network、database write、下一版本和持久后台 Agent。
过期授权不能发起新 proposal、model reservation 或 tool reservation。

恢复所需的 `SystemReconciliationAuthority` 也是 factory-only，但它不是新用户权限：它只绑定
一个既有未决 reservation、当前 writer lease、run/project/auth/proposal/preview digest，最多
执行一次 `record_reserved_terminal`、`mark_recovery_required` 或
`append_recovery_supervision`。它不能提出新工具、扩大路径或重做效果。

## 5. Capability Registry 与 Tool Gateway

首批 v10-D 能力使用 closed JSON Schema，记录 risk、reversibility、scope、authority、timeout、
max output、idempotency、verification 和 executor kind。已验证的核心本地能力包括：

- 只读：`project.file.read`、`project.tree.list`、`project.text.search`、
  `database.inspect.readonly`、`git.status.read`、`git.log.read`、`git.show.read`、
  `git.diff.read`；
- 受控执行：`project.file.patch`、`project.file.create`、
  `project.temp.dispose_owned`、`project.change.rollback`、`process.template.run`；
- `test.run.allowlisted` 保留 closed schema 但固定为 `disabled`；在尚未具备 OS 级
  no-network/read-only 测试沙箱前，不能通过 availability override 激活；
- `external.codex.handoff` 仅生成不可信 handoff artifact，不授予执行权。

Registry 中存在不等于当前任务获得授权；availability、Authorization、Plan node 和风险上限必须
同时成立。`Tool Gateway` 的 preview 与 execute 都重新验证相同绑定，防止 preview 后路径、
可执行文件、模板、Plan 或 Authorization 漂移。

## 6. 路径、命令与输出安全

路径策略以解析后的 Windows 项目根为准，拒绝：

- 相对逃逸和 `..` traversal；
- UNC、device path、ADS、drive switching、通配符和绝对路径注入；
- Windows reserved device names、尾随点/空格与大小写前缀混淆；
- symlink、junction/reparse point、嵌套 `.git` 和写入 hardlink；
- preview 后 file identity、size、mtime 或 SHA-256 漂移；
- 不属于当前 run 的临时文件或 backup reference。

命令策略不接收 shell 字符串。模型只能提交 typed arguments，代码将其解析为受控
`template_id`、绝对 executable identity、固定 argv 和最小环境；默认 `shell=false`、
`stdin=DEVNULL`。`&&`、`||`、pipe、redirect、command substitution、未授权环境变量、交互输入和
隐式网络不能成为合法模板参数。执行前还会重算模板 digest 与 executable identity。

stdout/stderr 有内存和 artifact 双重上限；结果保存摘要和受控 artifact reference。敏感值在
返回、artifact 和日志路径上均执行过滤或 fail-closed。SQLite 检查使用 `mode=ro`、
`PRAGMA query_only=ON`，并在打开前后核对主库与 WAL/SHM sidecar 的 identity、size 和
SHA-256；不使用会忽略 WAL 的 `immutable=1`，也不接受任意 SQL。

## 7. 执行、验证与接受的四层状态

v10-D 保持以下状态严格分离：

| 层级 | 证据 | 能否完成 Plan / 项目 |
| --- | --- | --- |
| Agent reported | 模型或 Agent 文本 | 否 |
| Tool executed | terminal tool execution / receipt | 否 |
| System verified | 独立重读、hash、query、Git 或 process artifact | 可完成 Plan node，不能代表接受 |
| User accepted | v9 权威 acceptance event | 才可按 Acceptance Policy 更新完成状态 |

写操作的 verifier 会核对当前文件 SHA-256、before backup record、storage key 和 receipt；
只读操作会重新读取；进程操作会核对固定模板、resolved argv、输出 digest 与受控 artifact。
executor 自报 `verification_passed=true` 不能伪造独立 `passed`。

## 8. 真实沙箱验收

定向测试在独立临时项目与临时 Operational DB 中完成了真实本地效果：

- 真实只读：读取文件一次、独立重读验证、run 进入 `waiting_acceptance`；
- 真实写入：对临时 `README.txt` 应用小型 patch，生成安全 backup record 与 `.before.bin`，
  独立比对 after SHA-256 后进入 `waiting_acceptance`；
- 真实回滚：使用当前文件 SHA-256 和本 run backup reference 恢复原内容；
- 越权：写入 `v10-G` 意图在 tool execution 前被拦截，文件保持不变；shell、network、
  database write、merge、tag、push proposal 全部 fail-closed；
- replay/restart：持久化 model reservation 后重启不会再次发送；未决 tool reservation 不会
  重复效果；
- cancellation：factory-sealed user cancellation 绑定 run/user/session/CSRF digest；若存在未决
  model/tool/verification，先完成精确 quarantine/reconciliation，再保持 `recovery_required`；
- bounded replan：仅当独立验证证明失败且没有 owned change、checkpoint 安全、授权仍有效时，
  允许同一 Objective/Plan/node/Authorization 的一次 digest-only observation replan；
- 卡死：测试时钟将 20 分钟门禁缩短为 40 ms，owned process 被终止且无残留。

本次文档复验命令：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_v10_*.py' -v
```

结果：444 collected，444 passed，0 skipped，0 failed，0 errors，441.878s。完整日志：
`output/v10-d-local/unittest-v10-final.log`；SHA-256：
`0000000000000000000000000000000000000000000000000000000000000000`。

## 9. Codex Adapter

本机只证明 Codex 桌面应用包资源可见；当前 PowerShell token 执行 `--help` / `--version` 均为
access denied，不能证明存在受支持、可脚本化、非交互的 CLI/Agent 接口。因此 v10-D 诚实固定为
`handoff_only`、`executor_available=false`、`reason_code=codex_cli_access_denied`。

Handoff artifact 只包含 Objective/Plan/Delegation digest、最小 context 引用、授权根目录和禁止项；
它不含 secret、任意命令、authority、execution result 或 acceptance。任何 execute 请求均返回
`codex_execute_unavailable_handoff_only`。详见
[Codex Adapter v10-D](./codex_adapter_v10_d.md)。

## 10. 已知限制与后续门禁

- v10-D Agent Loop 的节点验收使用 deterministic fake Runtime bridge；本节点没有新增真实
  Provider 调用。v10-C 的真实 DeepSeek transport/receipt 证据不被改写成 v10-D Agent 执行证据；
- 生产 Operational State DB 尚未迁移 schema `10001`；生产写通道保持关闭；
- v10-E 的认知任务工作区、desktop/mobile/PWA 浏览器验收尚未发生；
- v10-F 的真实 Provider + 原生 Agent 沙箱场景、生产 migration、恢复包、最终全量回归、main
  fast-forward 和 annotated tag 尚未发生；
- 当前 `Codex Adapter` 仅 handoff-only；未来若获得受支持接口，必须重新完成 help、协议、沙箱、
  token、stall 和独立验收，不能直接改写 availability；
- 本节点没有修改长期画像；学习候选与用户接受仍沿用既定分层边界。
