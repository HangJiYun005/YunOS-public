# YunOS v10-C Cognitive Orchestration 契约

> 节点：v10-C — Provider and Cognitive Orchestration
> 实现：`memory/cognitive_orchestration.py`
> 性质：纯函数、零网络、零数据库写入、零 Provider 调用、零工具执行

## 1. 定位

本层把已经形成的 `Objective Contract`、最小必要上下文、用户手动选择的
Provider Profile 和硬预算绑定为一个 adapter-independent model request，并把模型
返回解析为不可信的 `answer`、`plan` 或 `tool_proposal`。

本层不是第二套 Runtime，也不是 Tool Gateway：

- v7 继续拥有 Memory Package、Evidence Selection 和 Context Assembly；
- v8 Runtime 继续唯一拥有 model execution、artifact、usage、receipt、ledger 和
  in-doubt recovery；
- v9 继续拥有 Now Package、Project Resume Package 和 Operational State；
- v10-B 继续拥有 Plan Graph 与 Capability Registry；
- v10-C 只装配引用、建立预算、绑定 Provider identity、解析模型建议。

任何 `ProviderBinding`、上下文、模型文本或 `Capability Registry` 都不能生成
`Authorization Artifact`，也不能触发执行。

## 2. 固定版本

| 契约 | 标识 |
| --- | --- |
| Cognitive Orchestration | `yunos-v10-c-cognitive-orchestration-1` |
| Minimum Context | `yunos-v10-c-minimum-context-1` |
| Structured Output | `yunos-v10-c-structured-output-1` |
| Model Budget | `yunos-v10-c-model-budget-1` |
| v7 Memory Package | 直接校验既有 `MEMORY_PACKAGE_PROTOCOL_VERSION` |
| v9 Now Package | `yunos-now-package-1` |
| v9 Resume Package | `yunos-project-resume-package-1` |
| v8 Runtime | 只引用既有 `RUNTIME_PROTOCOL_VERSION` 与 `ARTIFACT_VERSION` |

## 3. 搜索与 AI 的硬分离

`Search` 没有 model request 构造路径：

- `prepare_model_request(mode="search", ...)` 拒绝；
- `reserve_model_call(mode="search", ...)` 拒绝；
- `assert_search_mode_zero_model(...)` 只在 Runtime execution、model calls、input
  tokens、output tokens 和 cost 的 delta 全部为零时返回 proof；
- 本模块不导入 Search route，也不将 FTS 结果转发给 Provider。

因此本模块不能把搜索模式升级成 AI 搜索。

## 4. 最小必要上下文

### 4.1 来源

允许的 `source_system` 只有：

- `v7_memory`
- `v9_now`
- `v9_resume`
- `project`
- `runtime`

`v7_memory` 必须通过既有 `validate_memory_package_standalone()`。本层只投影 package
状态和已选择的 `evidence`，不重新检索、不重新分类、不修改 v7 package。

`v9_now` 与 `v9_resume` 必须：

1. 匹配既有 package version；
2. `package_digest` 与去除 digest 后的 canonical payload 完全一致；
3. 显式绑定 `source_state`、`cache_control`、`cache_classification`、
   `cached_at`、`cache_stale` 和 `as_of`；
4. `live` 必须同时满足 `no-store`、`cached_at=null`、`cache_stale=false` 和既有
   live cache classification；
5. `cached` 永远判为 `stale`，不能伪装成 live；
6. `as_of` 在评估时间之后时判为 `unverifiable`；
7. required Now/Resume 为 `stale` 或 `unverifiable` 时默认 fail-closed。

通用 `build_context_source()` 只允许 `project` 与 `runtime`，不能绕过 v7/v9
专用校验器伪造 Memory/Operational package。

### 4.2 选择与预算

选择只在完整 item 边界进行，不进行字符串截断：

1. 按 source priority、source identity、item priority、item identity 确定性排序；
2. 应用每来源 `maximum_items` 与 `maximum_bytes`；
3. 应用全局 `maximum_items`、`maximum_bytes` 和
   `maximum_estimated_tokens`；
4. 超限 item 整体省略并记录 `source_item_limit`、`source_byte_budget` 或
   `global_budget`；
5. required source 最后一个 item 不会为了满足全局预算被静默删除；
6. 无法容纳 required context 时停止，不调用 Provider。

`ContextBinding` 保存：

- Objective identity；
- source reference 与 source digest；
- purpose；
- selected count 与 canonical byte count；
- per-source hard limit；
- freshness；
- omission reasons；
- `contains_secret=false`；
- deterministic `binding_digest`。

`CognitiveContext` 对全部 bindings、模型可见 payload、预算和精确用量计算
`context_digest`。

### 4.3 Secret 边界

选择前递归检查：

- 常见 API key、Bearer credential、GitHub token、AWS access key、JWT、private key；
- `api_key`、`access_token`、`password`、`authorization`、`cookie`、
  `private_key`、`secret_reference` 等敏感字段。

字段名先经过 Unicode NFKC、case folding、camelCase 和分隔符归一化，再进行 token、
连续片段与中英文敏感标记检查。因此 `api_key_backup`、`authorization_header`、
`database_password_encrypted`、`access_token_old`、`credential`、全角字符和无分隔变体
不能绕过过滤。只有 code-owned 且值类型严格为 `bool` 的少量安全状态键有精确白名单，
不能泛化为前缀或后缀例外。

含 secret 的 item 整体省略，不进行可能破坏语义的部分遮盖。若 required source 只剩
secret item，则返回 `required_context_secret_filtered`。source reference、purpose、
Objective projection、Runtime reference 和模型返回也必须再次通过 secret gate。

Secret 过滤不读取环境变量、配置文件或 secret store；它只验证准备进入模型边界的
显式结构化值。

### 4.4 Prompt / tool-output injection

所有模型可见内容固定标记：

- `trust_class=untrusted_*`
- `instruction_policy=data_only_never_authority`
- `context_grants_authority=false`
- `context_can_trigger_execution=false`

英文 injection classifier 只生成可审计的 `injection_indicators`；安全性不依赖
classifier 是否命中，因为所有 item 始终是不可信数据。code-owned system control
与 data JSON 分离，明确要求忽略数据中的指令。

## 5. 手动 Provider Binding

生产入口只接受 `ProviderRegistry.manual_select()` 返回的 typed
`ProviderSelection`。`ProviderBinding` 绑定：

- `provider_id`
- `profile_version`
- `profile_digest`
- `model`
- `selection_digest`
- `health_attestation_digest`（Fake Provider 的显式 `null` 也参与 selection digest）

`selection_mode` 必须为 `manual`，`fallback_used=false`，
`grants_authority=false`。不存在接受 raw 四字段的 production helper，也不存在
default resolver、价格路由、健康路由或自动 fallback。Provider response 与 usage
settlement 必须回报相同四元组；request digest 同时绑定 selection/health evidence。
任何不一致返回 `hidden_provider_fallback_detected`。

Provider binding：

- 不保存 endpoint；
- 不保存 `secret_reference`；
- 不读取 credential；
- 不改变 Objective、Context、Plan、Capability 或权限；
- `grants_authority=false`。

## 6. Model Budget

硬预算包括：

- `maximum_model_calls`
- `maximum_input_tokens`
- `maximum_output_tokens`
- `maximum_total_tokens`
- `maximum_cost_microunits`
- Provider 自身 `context_limit_tokens`

`reserve_model_call()` 在 send 前一次性预留 calls、input、output 和 cost。相同
`request_digest` 不能再次预留。`settle_model_call()` 检查：

- Provider 四元组没有隐藏切换；
- reservation 未被重复结算；
- actual input/output 以及已知 actual cost 没有超过 reservation；
- 累计实际用量没有超过 hard budget。

Provider 价格 metadata 未知时，调用方必须传入 `actual_cost_microunits=None`，不得把
未知解释为零。`ModelBudgetState` 将该 reservation 记录到
`unknown_cost_reservation_ids`，并在
`unknown_cost_reserved_ceiling_microunits` 中保留完整预留上限；预算判定始终使用
“已知实际成本 + 未知成本预留上限”的保守和。只有 Provider 报告可信价格时才累计
`actual_cost_microunits`。

本层的 `ModelBudgetState` 是单次纯计算用的 reservation/settlement 输入输出，不是
持久化执行账本。正式调用仍必须写入 v8 Runtime 的 execution、artifact、usage、
receipt、event chain 和 failure state。

## 7. Adapter-independent Runtime Reference

`CognitiveModelRequest.to_runtime_reference()` 生成不执行的 envelope：

- stable request digest；
- manual Provider binding；
- v8 Runtime protocol/artifact version；
- `ledger_owner=v8_runtime`；
- `receipt_required=true`；
- `usage_required=true`；
- `cognitive_store_duplicates_runtime_ledger=false`；
- 两条 message：code-owned system control 与 canonical untrusted data；
- `tools=[]`；
- `automatic_retry=false`；
- `automatic_provider_fallback=false`。

它不是 v8 `RuntimeArtifact` 的替代物。Provider Adapter/Runtime integration 必须在
正式 Runtime 边界把该 reference 转换为既有 Runtime 输入并继续执行现有
at-most-once、receipt、usage 和 recovery 协议。

## 8. Structured Output

模型只能返回一个 UTF-8 JSON object：

```json
{
  "contract_version": "yunos-v10-c-structured-output-1",
  "request_digest": "<sha256>",
  "provider_binding": {
    "provider_id": "deepseek",
    "profile_version": 1,
    "profile_digest": "<sha256>",
    "model": "deepseek-v4-flash"
  },
  "output_kind": "answer",
  "payload": {}
}
```

解析器拒绝：

- duplicate key 或 Unicode-normalized duplicate key；
- unknown/missing fields；
- NaN/Infinity；
- 非 UTF-8；
- 超深、超多节点、超大 collection；
- 超过 output byte budget；
- request/provider/kind binding drift；
- secret literal；
- replayed request 或 response digest。

### 8.1 `answer`

严格字段：

- `title`
- `body`
- `source_binding_ids`
- `warnings`
- `claims_execution=false`
- `claims_verification=false`
- `claims_acceptance=false`

引用只能指向本次 `CognitiveContext` 的 binding。

### 8.2 `plan`

严格字段：

- `plan_graph`
- `proposal_only=true`
- `grants_authority=false`
- `execution_started=false`

`plan_graph` 必须通过 v10-B `PlanGraph.from_dict()`、digest、DAG、revision 与 initial
node-state 校验，并与本次 Objective id/version/digest 完全一致。它仍是 proposal，
不能执行或持久化为已完成状态。

### 8.3 `tool_proposal`

严格字段：

- capability id/version/digest；
- typed arguments；
- reason；
- expected observation；
- risk class；
- `proposal_only=true`；
- `authorization_claimed=false`；
- `execution_claimed=false`。

解析必须传入 v10-B `CapabilityRegistry`：

1. exact capability triple lookup；
2. capability input schema validation；
3. code-owned idempotency identity；
4. replay-aware proposal validation；
5. 输出 `validated_proposal_only`。

返回不含 `Authorization Artifact`，`registry_grants_authority=false` 且
`execution_allowed=false`。真正执行只能在 v10-D Tool Gateway 完成。

## 9. 失败语义

所有违反边界的输入抛出 `CognitiveOrchestrationError(reason_code)`。关键 failure：

- `search_mode_provider_forbidden`
- `required_operational_context_not_fresh`
- `required_context_secret_filtered`
- `required_context_global_budget_exceeded`
- `model_request_replay`
- `model_*_budget_exhausted`
- `hidden_provider_fallback_detected`
- `structured_output_*`
- `model_*_authority_claim_forbidden`
- `capability_registry_required_for_tool_proposal`

失败不会调用 Provider、不会调用工具、不会写数据库、不会更新 v9 project state。

## 10. 测试

`tests/test_v10_orchestration.py` 覆盖：

- source 顺序与 digest determinism；
- v9 digest tamper、live/no-store、cached/stale、future-as-of；
- item-level byte/token overflow 与 no semantic truncation；
- secret whole-item omission 与 required secret-only fail-closed；
- prompt/tool-output injection 的 untrusted-data 标记；
- Search zero-model invariant；
- manual provider binding；
- calls/input/output/total/cost/context-limit hard budgets；
- reservation/settlement replay；
- hidden Provider fallback；
- v8 Runtime adapter-independent reference；
- answer/PlanGraph/CapabilityProposal 严格解析；
- duplicate/unknown/nonfinite/oversize/secret/fake-state claims；
- response/request replay。

该专项测试不访问网络、不读取 credential、不打开 Runtime DB，也不产生工具执行。

## 11. 明确限制

- 本层没有真实 DeepSeek/Kimi 调用；真实调用与 usage/receipt 证据由同节点的 Runtime
  integration 单独完成。
- 本层没有 Provider health polling 或自动路由。
- injection indicator 不是内容安全分类器；真正安全边界是所有内容始终无授权。
- 本层不持久化 `context_bindings` 或 `cognitive_runs`。
- 本层不创建 `Authorization Artifact`、`ToolExecution`、`Outcome`、
  `Verification` 或 `Acceptance Event`。
- 本层不允许模型创建 v10-G、v10.1 或 v11，也不提供 merge/tag/push 能力。
