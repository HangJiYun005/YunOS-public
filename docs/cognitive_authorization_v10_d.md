# YunOS v10 Authorization Artifact

本文是 v10 生产授权边界的简明权威索引。总体设计见
`docs/design/yunos_v10_bounded_cognitive_agency_baseline.md`，执行状态机与恢复语义见
`docs/bounded_execution_v10_d.md` 和 `docs/supervision_recovery_v10_d.md`。

## 1. 两类授权不能互相替代

YunOS 明确分开：

1. `CognitiveAuthorizationArtifact`：决定某个 Objective / Plan Node 可以读取、写入和调用哪些
   capability；
2. `ExternalSendConsent`：只允许把某个已经披露、已经预算、已经绑定 Provider 的最小请求发送
   一次。

模型外发同意不授予项目写入、Delegation、状态更新或结果接受。项目写入授权也不能暗中授权
Provider 调用。两者都不能由模型响应、聊天历史或工具输出生成。

## 2. Authorization Artifact 的固定绑定

每个 `act` run 的 artifact 至少绑定：

- Objective、Plan、Plan Node、Project 与各自 canonical digest；
- exact read roots / write roots；
- allowed / denied capabilities 与固定 command templates；
- risk ceiling、step / duration / model-call / token / cost budgets；
- database、network、commit、merge、tag、push、next-version 与 background-agent 权限；
- valid-from、expiry、rollback 和 acceptance policy；
- 用户 Authority Artifact 与最终 `authority_digest`。

默认 `database_write=false`、`network=false`、`commit=false`、`merge=false`、`tag=false`、
`push=false`、`next_version_creation=false`、`persistent_background_agent=false`。生产 Agent
不继承本次 Codex 工程任务的授权。

## 3. 签发与校验

生产写任务首版使用本机 CLI / local broker；不开放 Tailnet Web 写入口。签发边界依赖当前本机
OS 用户、固定 CLI confirmations、factory-sealed artifact 和一次性 run binding。它不把 remote IP、
User-Agent、隐藏按钮或单一 Origin 当作权限证明。

每次使用 artifact 时重新校验：

1. typed schema 与 factory seal；
2. Objective / Plan / Node digest；
3. Project 与 capability registry version/digest；
4. exact roots、路径身份、risk 与 command template；
5. expiry、预算、step reservation 与 replay state；
6. 当前 Provider selection 只影响模型路由，不影响权限；
7. Tool Gateway preview 与 execute 之间的 binding 未漂移。

任一未知、过期、缺失或冲突状态均 fail-closed。

## 4. 可信用户外发事件

v10-F 增加 disclosure-specific 的 `TrustedUserEventArtifact`。它记录的是本地 release controller
观察到 exact CLI confirmation 的 `observed_at`，不会伪造为原聊天消息的发送时间。事件绑定：

- 用户授权文本 SHA-256；
- 当前 Runtime disclosure digest；
- CLI / Objective / turn confirmation binding digest；
- 唯一 event id、evidence source 与 event digest。

每个模型调用需要独立事件和独立 consent；事件不能跨 disclosure 重放。Web/Tailnet 没有该签发
入口，移动端也不能生成、扩大或接受授权。

## 5. 结果接受仍是独立边界

Authorization 只允许执行，不代表任务已经成功。Runtime success、Tool executed、System verified、
User accepted 是四个不同状态。Agent 不能签发 Acceptance Event，不能把自检 `GO` 写成用户接受，
也不能在未满足 acceptance policy 时更新 v9 项目完成状态。

## 6. 实现索引

- `memory/cognitive_authorization.py`
- `memory/cognitive_external_send.py`
- `memory/cognitive_execution_store.py`
- `memory/cognitive_agent_loop.py`
- `memory/cognitive_local_broker.py`
- `tests/test_v10_authorization.py`
- `tests/test_v10_runtime_bridge.py`
- `tests/test_v10_agent_loop.py`
