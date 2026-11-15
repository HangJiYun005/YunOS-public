# YunOS v10-D Codex Adapter：handoff-only 交付

## 1. 结论

本机未证明存在当前 PowerShell 执行令牌可调用、受支持且可脚本化的 Codex
CLI/Agent 接口。v10-D 因此固定采用：

- `adapter_mode = handoff_only`；
- `executor_available = false`；
- `reason_code = codex_cli_access_denied`；
- `cli_version = null`；
- `supported_executable_path = null`；
- `supported_noninteractive_entrypoint = null`；
- cwd、sandbox、approval、command allowlist、structured output、log、token budget、
  timeout 与 process protocol 均为 `unverified`；
- `real_delegation_performed = false`；
- `real_acceptance_performed = false`。

这不是“Codex 已接入”，也不是一个可执行 Codex Executor。真实沙箱执行仍由 YunOS
原生 bounded Agent Loop 与 Tool Gateway 承担。

## 2. 本地只读审计证据

审计环境为 2026-08-01、Windows PowerShell、工作目录 `E:\AyunOS`。审计未访问
网络，未修改仓库、配置、登录、ACL 或凭据，也未执行真实 Codex 委托。

`where.exe codex` 与 `Get-Command codex -All` 仅发现桌面应用包资源：

```text
C:\Program Files\WindowsApps\OpenAI.Codex_26.727.6591.0_x64__2p2nqsd0c76g0\app\resources\codex
C:\Program Files\WindowsApps\OpenAI.Codex_26.727.6591.0_x64__2p2nqsd0c76g0\app\resources\codex.exe
```

PATH 中的 `C:\Users\ExampleUser\AppData\Local\OpenAI\Codex\bin\3e42d49ad3e35a50`
仅含 `rg.exe`，没有 `codex`/`codex.exe`；WindowsApps alias 路径中也没有可调用的
`codex.exe`。

以下本地调用全部以 exit code 1 和 PowerShell“拒绝访问”结束：

- `codex --version`；
- `codex --help`；
- `codex.exe --version`；
- `codex.exe --help`；
- 两个应用包资源文件的绝对路径 `--version` / `--help`。

应用包 ACL 的执行 ACE 带 `WIN://SYSAPPID Contains
"OpenAI.Codex_2p2nqsd0c76g0"` 条件；普通当前 shell 只能读取。这证明资源可见不等于
受支持的 shell CLI。由于顶层帮助不可执行，本次没有猜测或尝试任何子命令。

二进制只读证据：

| 项目 | 值 |
|---|---|
| `codex.exe` size | `358182192` |
| `codex.exe` SHA-256 | `0000000000000000000000000000000000000000000000000000000000000000` |
| `codex.exe` Authenticode | Valid；OpenAI OpCo, LLC |
| extensionless `codex` size | `310333488` |
| extensionless `codex` SHA-256 | `0000000000000000000000000000000000000000000000000000000000000000` |

`26.727.6591.0` 仅是桌面包路径版本，不能写成 CLI 自报版本。现有 v10.0 设计中
关于 `codex exec`、cwd、sandbox、JSONL 与 output schema 的内容属于此前官方手册
观察，不是本机 `--help` 证明；v10-D 不据此提升 availability。

## 3. 代码契约

实现位于 `memory/cognitive_codex_adapter.py`，为纯内存、无副作用模块。

### 3.1 Code-owned audit snapshot

`build_code_owned_codex_audit_snapshot()` 不接受调用方参数，只能返回上述固定审计
状态。`CodexAdapterAuditSnapshot` 直接构造、`dataclasses.replace()` 改写，或提交与
代码快照不完全相同的序列化记录都会 fail-closed。`audit_digest` 为 snapshot identity
payload 的 canonical SHA-256。

### 3.2 Handoff artifact

`create_codex_handoff_artifact()` 只接受：

- Objective Contract digest；
- Plan Graph digest；
- Delegation Contract digest；
- 最多 16 个 digest-only 最小上下文引用；
- 规范化的 Windows 授权读取根和写入根；
- 额外禁止项。

Artifact 不接受或保存：

- prompt、用户原文或完整上下文；
- 任意命令、argv、环境变量或工作进程；
- secret、secret reference、token、cookie 或 credential；
- Provider 请求或模型输出；
- Tool execution result、receipt 或验证声明；
- authority、acceptance 或成功声明。

代码会自动加入不可移除的禁止项，包括 arbitrary shell、secret/credential access、
network、DB write、根外写、`git reset --hard`、clean、rebase、merge、tag、push、
authority escalation、self-acceptance、下一版本创建与 persistent background agent。
写入根必须位于某个读取根内部。路径字符串的纯语法检查会拒绝相对路径、`..`、UNC、
device path、ADS、通配符、Windows 保留名与 case-insensitive 重复根。

`artifact_digest` 是 canonical identity payload 的 SHA-256。反序列化要求 closed
schema、固定 `handoff_only`、三个权限布尔值全为 false、全部 mandatory prohibition
仍存在且 digest 匹配。Canonical digest 提供完整性检查，不是签名，也不将 artifact
变成可信授权。

### 3.3 Execute boundary

`CodexHandoffOnlyAdapter` 只能由
`create_codex_handoff_only_adapter()` 创建，并绑定 code-owned audit snapshot。任何
`execute(...)` 调用都抛出：

```text
codex_execute_unavailable_handoff_only
```

模块没有 execution-success 类型或返回路径。向序列化 artifact 添加
`execution_succeeded` 等未知字段会因 closed schema 被拒绝；把
`execution_allowed`、`grants_authority` 或 `grants_acceptance` 改为 true，即使重新计算
digest，也会因 scope violation 被拒绝。

## 4. 安全边界与已知限制

- Handoff artifact 只是供人工或未来受支持 External Agent Adapter 使用的最小转交材料；
  它本身不调用 Codex。
- 桌面应用可使用不等于存在 CLI/API；不得通过复制包内二进制、提权、修改 ACL、逆向
  工程、UI 自动点击或绕过登录来提升状态。
- 本交付没有验证非交互入口、cwd 限制、sandbox、approval、命令限制、结构化输出、
  日志、token/cost、timeout、stall 或进程协议。
- 后续若获得受支持接口，必须重新进行本地 help 与真实沙箱验收，绑定 Delegation
  Contract，并置于 YunOS Tool Gateway、Authorization、Supervision、Verification 与
  Acceptance 边界之后；不能直接修改本快照来伪造 available。
- 人工返回的 Codex 内容始终是不可信输入，必须由 YunOS 独立验证，不能自动等同于
  Tool executed、System verified 或 User accepted。

## 5. 测试

定向测试：

```powershell
& 'C:\Users\ExampleUser\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -B -m unittest tests.test_v10_codex_adapter -v
```

测试覆盖 code-owned snapshot、factory-only、直接构造、`replace`、serialized tamper、
伪造成功、secret、路径逃逸、mandatory prohibition、deterministic artifact、canonical
round-trip，以及所有 `execute` 调用 fail-closed。测试本身不调用 Codex、Provider 或
网络，也不改配置。

Codex 单组定向结果将随 v10-D 冻结树统一复验填写；本组只验证 handoff-only 契约，不把
Provider/Planning 的相邻测试重复计入 Codex 接入证据。
