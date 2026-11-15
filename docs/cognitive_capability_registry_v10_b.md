# YunOS v10-B Capability Registry 核心

状态：实现完成，默认关闭，仅提供 typed descriptor、确定性 registry、可行性匹配与
proposal validation。本文不声明 Tool Gateway、Authorization Artifact、local broker 或
生产 executor 已经开放。

## 1. 与 v10 baseline 的一致性

本实现审计并遵循：

- `docs/schemas/yunos_v10_cognitive_agency.schema.json` 的 `capability` 定义；
- `docs/sql/yunos_v10_cognitive_agency_blueprint.sql` 与
  `memory/cognitive_schema_10001.sql` 的 `capability_registry` 字段、枚举和预算；
- `docs/design/yunos_v10_bounded_cognitive_agency_baseline.md` 的
  “模型只提出 typed capability、executor 由代码拥有”边界；
- `docs/security/yunos_v10_threat_model.md` 的 schema confusion、secret、path、command、
  replay、budget、verification 与 unavailable fail-closed 要求。

`CapabilityProfile.to_dict()` 的 canonical document 恰好包含现有 JSON Schema 要求的
16 个字段：

1. `capability_id`
2. `display_name`
3. `version`
4. `input_schema`
5. `output_schema`
6. `risk_class`
7. `reversible`
8. `read_scopes`
9. `write_scopes`
10. `required_authority`
11. `timeout_ms`
12. `max_output_bytes`
13. `idempotency`
14. `verification_method`
15. `availability`
16. `executor_kind`

`capability_digest` 是这 16 个字段的 canonical SHA-256，存储时对应 SQL 的独立
`capability_digest` 列，不被塞进 `canonical_capability` JSON。

## 2. Closed typed contract

`memory/cognitive_capability.py` 只接受一个有界 JSON Schema 子集。根必须为 object；
每个 object 必须显式 `additionalProperties=false`；string 和 array 必须有硬上限；
schema 深度、节点数、schema bytes、typed argument bytes、timeout 和 output bytes 都有
固定上限。拒绝：

- 未知 JSON Schema keyword、`$ref`、regex pattern；
- 类型不一致的 `enum` / `const`；
- duplicate normalized key、unknown field、缺 required field、NaN/Infinity；
- secret/credential/token/env/password 字段和常见 secret literal；
- 未声明的 extra argument。

schema 及 profile 内部会递归冻结。Registry 创建后只有 immutable snapshot，没有
`register()`、动态 discovery 或隐式 executor binding。

## 3. 永久禁止的能力形状

Capability id、display name、scope 和 schema property 会拒绝任意 command surface，包括：

- arbitrary shell、PowerShell、cmd 或 argv；
- `rm` / delete；
- `git reset`、`git rebase`、force push；
- network、HTTP、socket、download、upload；
- arbitrary database SQL；
- cross-root / outside-root。

路径参数只能使用受控 `relative_path`。绝对路径、Windows drive、UNC、`..`、ADS、
空路径段及 `.git/` control path 均在 typed argument validation 阶段拒绝。此校验不替代
未来 executor 的真实路径、junction/reparse 与 TOCTOU 双重检查；它只是更早的
fail-closed gate。

## 4. 只读与受控写不变量

无 `write_scopes` 的 profile 必须：

- `idempotency=read_only`；
- `required_authority` 为 `read` 或 `prepare`。

有 `write_scopes` 的 profile 必须：

- `required_authority=explicit_act`；
- 风险至少为 `medium`；
- `reversible=true`；
- `idempotency` 为 `idempotent` 或 `exactly_once`；
- verification 非空；
- executor kind 只能为 code-owned `native` 或隔离的 `local_broker`。

这些字段仍只是执行前置要求，不是 authority。Registry、Profile、Proposal 和
Feasibility 的 authority/execution 属性永远为 false。

## 5. 首批建议 descriptor

| Capability | 类别 | 默认状态 | 关键边界 |
| --- | --- | --- | --- |
| `project.file.read` | 只读 | `disabled` | 单个相对路径、内容上限、SHA-256 |
| `project.tree.list` | 只读 | `disabled` | 相对根、最多 2,000 项、无重复 |
| `git.status.read` | 只读 | `disabled` | code-owned porcelain-v2 读取形状 |
| `project.file.patch` | 受控写 | `disabled` | before/after SHA-256、backup receipt、单文件相对路径 |

这些是建议 descriptor，不包含真实文件、Git 或 broker 执行代码。
`suggested_capability_profiles()` 和 `build_suggested_capability_registry()` 不探测环境，
不因二进制、凭据或端口存在而自动改为 available。调用者只能显式传入
`availability_by_id`；未知 capability override 会被拒绝。

Availability 是 canonical profile 和 digest 的一部分。同一
`capability_id + version` 不能把 `disabled` 原地改成 `available`：放入同一 Registry
会被判为 `capability_profile_drift`。合法 availability 演进必须创建显式的新 version，
并由后续受信控制面审核、持久化。

10001 executable schema 与 DESIGN ONLY blueprint 已在生产迁移前补齐
`capability_registry_no_update/no_delete` 两个 trigger。因此应用层拒绝之外，SQLite 也会
阻断同版本原地 UPDATE/DELETE。相同 id/version/digest 三元组具有完整 UNIQUE/FK 绑定，
`tool_proposals` 不能只替换 version 或 digest。

## 6. 确定性 identity、drift 与 replay

Profile digest 对 NFC-normalized、key-sorted、set-like tuple-sorted canonical payload
计算。Registry digest 绑定 contract version 以及排序后的
`capability_id + version + capability_digest` 清单。

Registry 在构造和 snapshot reopen 时 fail-closed：

- 同一 id/version/digest 重复：`duplicate_capability_profile`；
- 同一 id/version 不同 digest，包括 availability 原地变化：
  `capability_profile_drift`；
- snapshot digest 不一致：`capability_registry_digest_mismatch`；
- proposal 绑定 digest 不一致：`capability_profile_drift`；
- 已出现的 idempotency key：`capability_proposal_replay`。

Registry snapshot 只做 deterministic reopen，不提供增量 mutation。

## 7. Feasibility 与 Requirement

`CapabilityRequirement` 必须精确绑定：

- `capability_id`
- `capability_version`
- `capability_digest`

不支持 minimum-version 自动升级，也不在 feasibility 时隐式选 latest。匹配继续检查：

- manual availability；
- maximum risk；
- reversibility；
- allowed read/write scopes；
- maximum authority requirement；
- timeout/output hard budget；
- allowed idempotency；
- required verification；
- allowed executor kinds。

`CapabilityFeasibility.feasible=true` 只表示“当前 descriptor 满足这组静态要求”；
`registry_grants_authority=false` 且 `execution_allowed=false` 始终成立。它不能替代
Authorization Artifact、实时 expiry/budget 检查、path resolution、Tool Gateway policy、
execution receipt 或系统 verification。

## 8. Model proposal 边界

`CapabilityProposal` 绑定 capability id/version/digest、canonical typed arguments、
argument digest 与 deterministic idempotency key。模型可以提出 proposal；Registry
最多执行：

1. exact profile/digest lookup；
2. manual availability 检查；
3. closed JSON Schema 验证；
4. secret、path、argument budget 检查；
5. replay ledger membership 检查。

返回值固定为 `decision=validated_proposal_only`、
`registry_grants_authority=false`、`execution_allowed=false`。Registry 没有 execute
方法。真实执行必须由后续 Tool Gateway 同时验证用户签发 authority、实时预算、roots、
executor binding、idempotency ledger、receipt 与 verification。

## 9. 同库 Registry 与只读 API

`CognitivePlanningStore.install_builtin_capabilities()` 只接受 code-owned suggested
manifest，不接受模型上传或动态 discovery。安装必须满足：

- `user:*` authority；
- 精确 `capability_registry_install` 事件权限；
- active writer lease；
- trusted clock；
- Registry 完整集与 deterministic digest；
- 单一 `BEGIN IMMEDIATE`。

空 Registry 可一次性安装；完整 Registry 可幂等 replay；任何 partial、额外 profile、
同版本 drift 或 schema tamper 均 fail-closed。安装结果固定
`grants_authority=false`、`execution_allowed=false`。

普通 Web 只提供 `GET /api/cognitive/capabilities`，支持 risk/availability/executor/limit
白名单过滤；响应 `readonly=true`、`no-store`。没有 Registry POST，也不影响 Search。

同一 generation 10001 的生产前 schema hardening 把 extension object allowlist 从
14 tables + 14 triggers + 7 indexes（35）收紧为 14 tables + 16 triggers + 7 indexes
（37）；没有创建第二个 migration 或第六数据库。

## 10. 验证

定向测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_v10_capability tests.test_v10_planning_store tests.test_v10_planning_api -v
```

覆盖 JSON Schema/SQL 字段一致性、canonical digest、NFC、duplicate key、schema
poisoning、secret、budget、path traversal、arbitrary control surface、只读/受控写不变量、
registry immutability、availability drift、snapshot drift、exact requirement binding、
feasibility、proposal digest 与 replay，以及“model proposes, registry grants nothing”。
