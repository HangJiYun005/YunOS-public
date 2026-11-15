# YunOS v10-C Provider Registry 与 Adapter Contract

## 1. 定位

本节点建立模型无关的 `Provider Registry` 和纯 Adapter Contract。它负责回答：

- 当前有哪些明确命名、明确版本的 Provider Profile；
- 用户手动选择的是哪一个精确 Profile；
- 该 Profile 声明哪些能力、预算和健康状态；
- Adapter 收到的请求与返回结果是否绑定到同一 Profile。

它不负责：

- 读取密钥；
- 自动探测环境变量或私有配置；
- 调用网络；
- 写数据库；
- 创建 Runtime execution、receipt 或 ledger；
- 授予 Agency authority；
- 暗中选择默认模型或故障切换。

所有真实模型执行仍必须进入既有 v8 Runtime 的 execution、receipt、usage、artifact、ledger 和 failure state。Provider Registry 不是第二套 Runtime。

实现文件：

- `memory/cognitive_provider.py`
- `tests/test_v10_provider.py`

## 2. 现有 Provider Adapter 审计

本次只读审计了以下现有实现；没有读取密钥值，也没有发起真实 Provider 调用。

### 2.1 `memory/runtime_protocol_contracts.py`

这是当前生产 DeepSeek D1 Runtime 的纯协议边界：

- `provider=deepseek`
- `model=deepseek-v4-flash`
- `base_url=https://api.deepseek.com`
- 请求 artifact、wire body、delivery、profile binding、run、event、receipt 和 ledger 均有确定性 identity；
- 模块本身不访问文件、环境变量、数据库、凭据或网络。

v10-C 的 `deepseek` Profile 复用上述 provider、model 和 endpoint，不复制 Runtime ledger。

### 2.2 `memory/deepseek_v4_adapter.py`

这是当前受支持的 DeepSeek v4 transport：

- 只接收已经冻结并验证的 `PreparedRuntimeRequest`；
- 生产凭据只由既有私有文件解析器加载；
- Provider、HTTP、transport 和 response failure 对外使用净化后的错误码；
- transport 之外不负责记忆检索、授权或状态更新。

v10-C 不修改该 transport，也不在 Registry 中调用它。后续真实执行必须通过 v8 Runtime 绑定。

### 2.3 `memory/deepseek_adapter.py`

仓库仍有较早的 DeepSeek transport，其模型为 `deepseek-chat`，并支持环境变量或私有文件取密钥。v10-C 不把该旧路径设为生产 Profile，也不通过它建立隐式 fallback。

审计结论：复用 `deepseek_v4_adapter.py` 与 `runtime_protocol_contracts.py` 的正式 v8 边界；Registry 只保存可审计 Profile 和 opaque secret reference。

## 3. Provider Profile

`ProviderProfile` 是不可变、闭合字段、确定性 digest 的合同。字段如下：

| 字段 | 含义 |
|---|---|
| `provider_id` | 稳定 Provider 标识 |
| `display_name` | 用户可见名称 |
| `profile_version` | Profile 整数版本 |
| `adapter_kind` | `deepseek_v8_runtime`、`openai_compatible`、`local_openai_compatible` 或 `fake` |
| `adapter_version` | Adapter 合同版本 |
| `endpoint` | 无凭据、无 query、无 fragment 的 endpoint |
| `model` | 明确模型标识 |
| `capability_flags` | `chat`、`reasoning`、`structured_output`、`tool_proposals` 等能力声明 |
| `tool_calling_support` | `none`、`compatible` 或 `native` |
| `structured_output_support` | `none`、`json_object` 或 `json_schema` |
| `context_limit_tokens` | 上下文上限 |
| `timeout_ms` | 单次调用超时上限 |
| `reasoning_options` | 可手动选择的 reasoning 选项 |
| `cost_metadata` | 已知、未知或不适用的整数 microunit 价格元数据 |
| `enabled` | 配置层是否启用 |
| `secret_reference` | 可选 opaque resolver reference，不含 secret value |
| `health_state` | `healthy`、`degraded`、`unknown`、`unavailable` 或 `disabled` |
| `health_reason` | 闭合 reason code |
| `profile_digest` | 上述 canonical profile 的 SHA-256 |

远程 endpoint 必须使用 HTTPS，并拒绝 user-info、query 和 fragment。本地 Provider 只允许 HTTP loopback。Fake 只允许 `fake://contract/...`。

`to_dict()`、`from_dict()` 和 `to_record()` 使用同一个闭合 canonical object，并在对象内包含 `profile_version` 与 `profile_digest`；SQL `canonical_profile` 可直接保存该对象，同时由独立 `profile_digest` 列做重复绑定校验，不存在额外 wrapper 格式。

Profile 的 `grants_authority` 和 `executable` 永远为 `False`。

## 4. 初始 Profile

| `provider_id` | 用途 | 初始状态 |
|---|---|---|
| `deepseek` | 复用 v8 DeepSeek Runtime | 缺少 opaque credential reference 时 `unavailable`；reference 存在但未经 health evidence 时仍为 `unknown` |
| `kimi` | Kimi OpenAI-compatible 合同；当前文档 endpoint `https://api.moonshot.cn/v1`、model `kimi-k3` | 没有 `secret.kimi.compatible` 时明确 `unavailable`，不伪造真实调用 |
| `openai.compatible` | 未来手动配置的 OpenAI-compatible Profile | `disabled` |
| `local.openai_compatible` | 未来 loopback 本地模型 Profile | `disabled` |
| `fake` | 确定性 contract test | 生产默认 `disabled`；只有测试显式 `allow_fake=True` 才为 `healthy/test_only` |

Kimi 的 endpoint 和 model 按 2026-07-31 查验的官方文档固定为 `https://api.moonshot.cn/v1` 与 `kimi-k3`；实际 chat transport 将使用 `/chat/completions`。这仍只是首版兼容 Profile 合同，不代表已经完成真实 Kimi 健康检查或真实调用。没有 Kimi credential reference 时，`manual_select` 必须 fail-closed；本节点只完成合同与 Fake/mock 验证。

## 5. Secret Boundary

`SecretReference` 仅包含：

- `reference_id`
- `resolver_kind`
- `required`

它没有 `value`、`api_key`、`token`、`authorization`、`password` 或私钥字段。Profile 和 Registry 的 public serialization 只会输出 opaque reference，不会解析引用。

模块还拒绝：

- 常见 API key、Bearer token、AWS key 和 private-key literal；
- endpoint user-info；
- endpoint query 参数；
- 在闭合 Profile 中增加未知 secret 字段；
- 在 Adapter request 中直接放入可识别的 secret literal。

Provider 健康状态与 secret presence 是不同证据：

1. reference 缺失：`unavailable`；
2. reference 存在但尚未健康验证：`unknown`；
3. code-owned Profile 保持不可变；调用方提供的独立短期 attestation 才能证明本次选择时
   的 `healthy` 或 `degraded` readiness；
4. Registry 不读取 secret，也不改写 Profile 来“帮忙”升级健康状态。

## 6. Provider Health Attestation

真实 Provider readiness 不能用一个 caller 提供的 boolean 或字符串表示。`ProviderHealthAttestation` 是不可变、短期、确定性 digest 的健康证明，精确绑定：

- `provider_id`
- `profile_version`
- `profile_digest`
- `health_state`：仅 `healthy`、`degraded` 或 `unavailable`
- `checked_at`
- `expires_at`
- `evidence_digest`
- `attestation_digest`

规则：

- 最长有效期 900 秒；
- `checked_at` 和 `expires_at` 必须是规范 UTC timestamp；
- Registry 不读取系统时钟，调用方必须传入受信任的 `evaluated_at`；
- `evaluated_at < checked_at`、`evaluated_at >= expires_at`、错绑、digest 伪造和 `unavailable` 全部拒绝；
- attestation 只证明 Provider readiness，不授予 authority；
- Kimi 缺少 credential reference 时 Profile 本身为 `unavailable`，attestation 不能绕过；
- 只有代码内 `healthy/test_only` 的 Fake Profile 可以不带 attestation。

选择结果保留完整 `ProviderHealthAttestation`、创建时的 `health_evaluated_at` 和
`health_attestation_digest`，并把三者纳入 `selection_digest`。`ProviderSelection` 只能由
`ProviderRegistry.manual_select()` 的 code-owned factory seal 创建；任意直接构造或
`dataclasses.replace()` 伪造都会 fail-closed。后续 Runtime 在 preview 与 execute 边界
使用当前受信任 UTC 时间重新验证完整 attestation 的 Profile 绑定、状态和有效期，而不只
检查一个非空的 64 位摘要，也不需要把凭据或 readiness 原始响应写进 Profile。

## 7. Manual Selection Only

唯一选择入口是：

```python
selection = registry.manual_select(
    provider_id="deepseek",
    profile_version=1,
    profile_digest="<exact sha256>",
    health_attestation=attestation,
    evaluated_at="<trusted UTC timestamp>",
)
```

选择必须同时匹配：

- `provider_id`
- `profile_version`
- `profile_digest`

并且 Profile 必须：

- `enabled=True`
- 不是 `disabled` 或 `unavailable`；`unknown` Profile 必须具有精确绑定、仍在有效期内且
  health 为 `healthy` 或 `degraded` 的 attestation
- 所需 opaque secret reference 已被调用方声明为 configured

未知 Provider、版本不匹配、digest 漂移、disabled、unavailable、缺少有效 attestation
的 unknown Profile 或 reference 缺失全部拒绝。Registry 没有 `default_provider`、
`auto_select`、`fallback` 或按成本/速度自动切换路径。

`ProviderSelection` 明确记录：

- `selection_mode=manual`
- `fallback_used=False`
- `grants_authority=False`
- 完整 `health_attestation`
- `health_evaluated_at=<trusted UTC timestamp>`
- `health_attestation_digest=<exact attestation sha256>`
- 确定性 `selection_digest`

Provider 切换因此只改变模型绑定，不改变 Objective、Plan、Authorization 或 Capability authority。

## 8. Adapter Contract 与 Fake Provider

`ProviderAdapterRequest` 绑定：

- request identity；
- 精确 Provider Profile 四元组：`provider_id/profile_version/profile_digest/model`；
- 最小输入；
- response mode；
- reasoning option；
- output token budget；
- 是否允许模型提出 tool proposal；
- 确定性 request digest。

`ProviderAdapterResponse` 绑定同一四元组和 request digest，并包含：

- content；
- 可选 structured output；
- tool proposals；
- usage；
- finish reason；
- response digest。

Adapter 的返回仅代表 `Agent reported / Provider returned`。它不代表：

- Tool Gateway 已执行；
- System verified；
- User accepted。

`FakeProviderAdapter`：

- 完全本地；
- 不读文件、环境或 secret；
- 不访问网络；
- 对相同请求返回相同 response；
- 强制 exact Profile binding；
- 用于 structured output、usage、digest 和 binding failure 的 contract test。

Fake Profile 永远不能作为生产健康 Provider 的隐式 fallback。

## 9. 测试覆盖

`tests/test_v10_provider.py` 覆盖：

- 五类初始 Profile；
- v8 DeepSeek identity 复用；
- closed profile 与 deterministic digest；
- record round-trip 与 tamper；
- secret field/literal 拒绝；
- remote/local/fake endpoint 边界；
- capability/support 一致性；
- health/enabled 一致性；
- cost metadata；
- Kimi 无 credential fail-closed；
- manual exact selection；
- unknown provider 无 hidden fallback；
- credential reference 与 health evidence 分离；
- 短期 health attestation 的有效、过期、尚未生效、错绑、伪造和 unavailable 分支；
- caller boolean/string/mapping 无法伪造 readiness；
- selection digest 绑定 attestation digest；
- Registry 不授予 authority、不执行；
- Fake Adapter Protocol、binding、usage、structured output 和 digest；
- Fake response 不可变。

本节点没有执行真实 DeepSeek 或 Kimi 调用。真实 DeepSeek 验收属于 v10-C Runtime orchestration，并必须只运行一次正式受控调用和完整 v8 ledger 验证。
