# YunOS v10-C Cognitive Runtime Bridge

## 定位

`memory/cognitive_runtime_bridge.py` 是 v10-C 到既有 v8 Runtime 的最小安全桥；
`memory/cognitive_external_send.py` 提供纯函数、factory-only 的一次外传 consent 契约。
两者不创建数据库、表、账本或第二套模型执行路径。模型执行、artifact、receipt、usage 与
exactly-once 状态仍只由 `ProductionRuntimeExecutionService` 和原 Runtime ledger 持有。

生产入口为：

```python
bridge = ProductionCognitiveRuntimeBridge()
disclosure = bridge.prepare_disclosure(
    selection,
    request,
    reservation,
    request_id="request:...",
    mutation_id="mutation:...",
)
consent = create_external_send_consent(
    selection=selection,
    request=request,
    reservation=reservation,
    **disclosure.consent_binding(),
    # caller-supplied claim；可信 issuer 留待 v10-E/F
    user_utterance="...",
    granted_at="...",
    expires_at=disclosure.expires_at,
    ...
)
preview = bridge.preview(
    disclosure,
    selection,
    request,
    reservation,
    consent=consent,
)
authority = open_production_runtime_execution(
    execute=True,
    allow_external_model_call=True,
    confirm_deepseek_send=True,
)
reference = bridge.execute(
    preview,
    selection,
    request,
    reservation,
    consent=consent,
    confirmed=True,
    authority=authority,
)
```

`ProductionCognitiveRuntimeBridge()` 没有构造参数，不接受 path、service、adapter、transport、delivery builder 或 generation config 注入。真实 execute 只接受现有 `open_production_runtime_execution(...)` 签发的进程内 authority。

## Typed binding

生产 bridge 同时绑定：

- `ProviderSelection`：必须由 Registry factory 创建，手动选择、无 fallback、无 authority，
  并携带完整且当前仍有效的 health attestation；
- `ExternalSendConsent`：绑定 caller-supplied 用户原文 claim、披露 digest、Objective、
  Context、Provider selection、health attestation、request、reservation、单次调用、
  no-retry、no-fallback 与无写入/委托/状态更新/接受边界；还绑定 exact
  `ExternalSendDisclosure` digest、固定 `Small`、Runtime request/mutation IDs、question SHA、
  reviewed binding digest、artifact identity 与 model run ID；
- 固定 Provider：`deepseek`；
- 固定 model：`deepseek-v4-flash`；
- 固定 code-owned profile version 与 profile digest；
- `CognitiveModelRequest`：当前 v10-C 只接受 `consult` / `answer`；
- `ModelCallReservation`：sequence 为 1，`maximum_model_calls` 为 1；
- v8 固定输出上限：1024 tokens，bridge 不允许覆盖 `max_tokens`；
- v10-C bridge 的 delivery budget 固定为 `Small`，调用者不能选择其他档位；
- request、provider binding、reservation、consent 和 preview digest 必须完全一致。

Kimi、未来 OpenAI-compatible、本地 Provider、Fake Provider、raw provider 字段和无 health attestation 的选择全部 fail-closed。Provider 选择不会授予执行权限。

v10-C 的 pure factory 不验证 UI/session user-event issuer，固定记录
`evidence_source=caller_supplied_interactive_claim` 和
`trusted_user_event_verified=false`。模型没有 mint consent 的 capability，但任意受信代码
调用者仍可提供 claim；因此它只是 exact-send gate，不是生产授权来源。可信 user-event/
session artifact 的签发与验证必须在 v10-E/F 本地管理入口集成后才能用于最终接纳。

## 最小上下文与 500 字符边界

bridge 要求恰好存在以下三个已验证 `ContextBinding`：

- `v7_memory`
- `v9_now`
- `v9_resume`

每个 binding 必须与 `CognitiveContext.model_context` 中的 exact binding 和 data item 对应。question 使用固定单行格式 `YCD1`，明确标注：

- `control=untrusted-data,no-authority,no-execution`
- objective 的 `data` 与完整 SHA-256（base64url 表达）
- 每个 context 的 `source`、最小 `data` 摘要和完整 binding digest

这不是原始 user utterance，也不能被解释为 authority。摘要只从已经 secret-filtered 的结构化上下文中确定性提取，并移除字段分隔符。若完整 digest、来源标签、控制标记和最低摘要不能同时装入 v8 的 500 字符单行上限，preview 直接失败，不丢弃控制字段或截短 digest。

v10-C 的真实 one-shot discussion 证明了 transport/receipt 闭环，但模型明确指出该压缩
摘要不足以形成下一阶段方向建议。因此 500 字符信息密度是显式 carry-forward，不是最终
产品讨论验收；必须在 v10-F 的真实讨论场景前通过不削弱控制字段与 digest 的方案关闭。

## Disclosure preview 与 authorized preview

`prepare_disclosure(...)` 是 consent 之前唯一允许的步骤：

- 只读调用现有 Runtime preview；
- 不读取凭据；
- 不发送网络请求；
- 不持久化 execution；
- 返回 exact `runtime_reviewed_execution_binding`；
- 绑定 selection/request/reservation、三个 context binding、固定 `Small`、Runtime IDs、
  question SHA、reviewed binding、预期 v8 model run 与 artifact identity；
- 生成 factory-only、不可伪造的 `ExternalSendDisclosure` 与 deterministic digest。

consent 必须在 disclosure 的 `prepared_at` 之后签发，且不得晚于 disclosure/health expiry。
随后 `preview(disclosure, ..., consent=...)` 才生成 authorized `CognitiveRuntimePreview`；此步骤
不再调用 Runtime，也不产生网络或持久化副作用。authorized preview 具有独立 factory
seal，公开 constructor 与 `dataclasses.replace()` 不能伪造；consent 与 disclosure 任一字段
不一致即拒绝。

execute 会使用当前 UTC 时间重新验证完整 health attestation 与 typed consent 的有效期，
重建并验证 disclosure、重新生成 question、重验全部 typed binding 和 `bridge_digest`，再把
exact reviewed binding 交给 v8 Runtime。任意过期、错绑或 preview 漂移都 fail-closed。
同一 consent 无法改绑第二组 Runtime IDs；完全相同的 IDs/preview 仅走 v8 idempotent replay。

## Execute 与结果引用

execute 没有自动 retry，也没有 Provider fallback。首次真实成功必须满足：

- `adapter_call_count_this_request = 1`
- `model_call_count_for_run = 1`
- `exactly_one_call = true`
- `mock_transport = false`
- `network_request_count = 1`
- `credential_read = true`
- `automatic_retry = false`

完全相同的 idempotent replay 复用 v8 receipt：

- `adapter_call_count_this_request = 0`
- `model_call_count_for_run = 1`
- `exactly_one_call = true`
- `network_request_count = 0`
- `credential_read = false`

bridge 在返回前使用 `get_run(model_run_id)` 重新读取 v8 权威结果，要求 execute envelope 与 ledger projection 完全一致，并验证 receipt identity、artifact identity、Provider、model、request/mutation binding 和 usage token ceiling。

`CognitiveRuntimeExecutionReference` 只返回：

- v8 `model_run_id`
- v8 `receipt_identity`
- v8 `artifact_identity`
- v8 usage digest
- status / execution phase
- one-shot transport evidence
- token budget verification

它不复制 model response、receipt、artifact 或 usage payload。当前 code-owned DeepSeek
profile 的价格 metadata 为 unknown，因此 bridge 诚实返回
`provider_cost_metadata_unavailable`；orchestration settlement 记录
`actual_cost_microunits=None` 并保留完整 `unknown_reserved_ceiling`，不把未知成本伪装为
零。调用次数、token ceiling 与预留成本 ceiling 仍为硬边界。

## Mock 与故障注入

唯一允许 path 绑定的入口是：

```python
create_mock_cognitive_runtime_bridge(temp_sidecar, scenario="success")
```

它只连接显式临时 Runtime sidecar，使用现有 v8 mock service，不读取生产凭据、不产生 Provider 网络调用。支持的确定性测试场景为：

- `success`
- `preview_drift`
- `provider_failure`
- `usage_over_budget`

故障场景只用于验证 bridge fail-closed/no-retry，不可用于生产。测试覆盖 typed
selection/profile 错绑、缺失 v7/v9 binding、one-call 与固定 token budget、disclosure 时序、
factory forgery、question/artifact/reviewed binding 漂移、跨 Runtime IDs consent 重放、
execute-boundary expiry、explicit confirmation、authority、provider failure、usage 超预算、
首次执行和 idempotent replay 零新调用。

## 安全结论

- Runtime ledger 唯一所有者：v8 Runtime；
- 新数据库：0；
- 新 schema/migration：0；
- 任意 Provider fallback：0；
- 自动 retry：0；
- raw Provider：拒绝；
- generation override：拒绝；
- preview side effect：0；
- response/receipt/artifact/usage 复制：0；
- production dependency/path/adapter injection：0。
