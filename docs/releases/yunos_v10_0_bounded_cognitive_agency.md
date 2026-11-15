# YunOS v10.0 — Bounded Cognitive Agency

## 1. 发布定位

YunOS v10 在 v7 长期记忆、v8 Trusted Runtime 与 v9 Operational State 之上，
增加一层受限认知代理能力：把用户的明确目标解释为 `Objective Contract`，形成可验证的
`Plan Graph`，在独立 `Authorization Artifact` 约束下通过 `Tool Gateway` 执行，并将
Agent 报告、工具执行、系统验证与用户接受严格分离。

本版本的权威范围仅包含 `v10.0`、`v10-A`、`v10-B`、`v10-C`、`v10-D`、`v10-E`
和 `v10-F`。它不是无限自治 Agent、数字人格复制、自动人生规划、第二套 Runtime、
第二套 Operational State 或第二套 Evidence System。

本 tracked 文档冻结源码层发布契约。恢复包与独立恢复必须生成正式 manifest、verify report
和 acceptance evidence；这些持久化 artifact 的摘要由封板 operator evidence 引用。最终
`main`、annotated tag object 等无法写回其自身 tracked 提交的自引用值，仅保存在 Git 忽略的
operator evidence 中并在最终回报核对；本文件不预写尚未生成的值。

## 2. 七个正式节点

| 节点 | 主要交付 | 节点提交 | Gate |
| --- | --- | --- | --- |
| v10.0 | 架构基线、ADR、Threat Model、JSON Schema、DESIGN ONLY SQL blueprint | `346735061d8c190600073e63cc1f8bf6d766d63e` | GO |
| v10-A | Objective Contract、意图/歧义/澄清、Agency Level、Cognitive Stance | `5f2d9cec8cccebdc1f5477b2aa886a34581ba1b7` | GO |
| v10-B | Capability Registry、Plan Graph、revision、feasibility、workflow candidate | `9c449a500ef5e2cdb09ba632960dec2fc84eea82` | GO |
| v10-C | Provider Registry、DeepSeek/Kimi-compatible adapter、预算上下文、Runtime receipt | `697a7589aa202e279abb1d6c5ff798d313117ed1` | GO |
| v10-D | Tool Gateway、Authorization、原生 bounded Agent Loop、监督/恢复/越权阻断 | `dcd669883cfc1ff1741fce48851d6ab9d9f7755f` | GO |
| v10-E | 简体中文认知任务工作区、桌面监督栏、移动端只读呈现 | `9389728be82a7d5f3a3d2787809743a391e7e4ab` | GO |
| v10-F | schema 10001 生产接纳、真实 Provider/计划/执行、恢复与最终候选 | `6d1973bfa06be468eb6a7cf21429b70449b2a2e5` 起，后续封板修正到 `3de64064e33436d11155d8949168c00a4480c65e` | GO |

后续封板修正保持 v10-F 范围不变，解决跨进程恢复、恢复包身份、Windows Python
redirector 与真实服务 PID 的 owned-process-tree 验证；最终代码提交之后只有发布文档闭环，
没有改变已全量验证的代码树，也没有增加 v10-G、v10.1 或 v11。

## 3. 搜索与 AI 的硬分离

搜索模式继续走本地 SQLite / FTS / Operational State：不装配模型上下文，不进入
Provider，不产生 Runtime 模型 execution，也不产生 API 费用。真实关键词验收返回 5 条
本地结果，Provider 与 Runtime 模型计数增量均为 0。

AI 模式才建立 Objective、预算化上下文、Provider 调用、Plan 或 Tool Proposal。
上下文包含来源与 digest，排除密钥、全量数据库和无关私人状态；缓存内容不会伪装成实时状态。

## 4. 认知与授权模型

- `agency_level` 为 `consult | plan | prepare | act`；模型不能自行提升等级。
- `cognitive_stance` 为 `continue | explore | breakthrough`；姿态只影响方案选择，不扩大权限。
- Objective、Plan、Authorization 均有确定性 canonical digest 和版本。
- `act` 必须绑定项目、读写根、能力、命令模板、风险上限、步数/时间/模型预算与到期时间。
- 默认 `merge=false`、`tag=false`、`push=false`、`network=false`、`database_write=false`。
- Agent 不得改变根目标、越过人工门禁、自行接受结果或创建下一版本。

## 5. Capability Registry 与 Tool Gateway

模型只能提出 typed tool proposal；实际执行由 YunOS 校验 schema、Objective、Plan node、
Authorization、风险、规范化路径、symlink/junction、命令 allowlist、秘密过滤、输出上限、
receipt 与 verification。

首版不开放任意 shell、任意 PowerShell、任意 SQL、任意网络下载、跨根写入、
`reset --hard`、`git clean`、rebase、merge、tag、push 或系统/Tailscale 修改。真实沙箱任务
完成了受控文件变更、测试、diff、验证、等待接受与回滚演练；越权路径、任意 shell、
数据库、网络、merge/tag/push 和下一版本扩张全部 fail-closed，并产生 overreach evidence。

## 6. Provider 与 Runtime

Provider Registry 为模型无关结构，保存 endpoint、model、capability、structured output、
tool calling、上下文/超时/推理/成本元数据、secret reference 与 health state。Provider
选择是显式的；没有隐藏自动切换，切换不改变记忆、状态或权限。

- DeepSeek：在用户知情批准最小必要外发边界后完成真实 discussion 与 bounded-agent 验收。
- Kimi-compatible：profile、adapter、结构化输出和 mock/contract 测试完成；当前无 Kimi
  凭据，因此安全标记不可用，没有伪造真实调用。
- 真实验收共 3 次 DeepSeek POST、4 次 readiness GET；prompt 6,752、completion 1,133、
  total 7,885 tokens。所有真实调用进入既有 v8 Runtime execution、receipt、usage、artifact、
  ledger 与 failure state；没有第二套模型账本。
- 密钥未进入 Git、SQLite 明文、前端、日志、截图或发布报告。

## 7. 原生 Agent、Codex Adapter 与恢复

原生 bounded Agent Loop 完成 `Objective → Context → Plan → Proposal → Policy → Tool →
Observation → Replan → Verification → Outcome → Acceptance` 的真实闭环。Cognitive Run 持久化
checkpoint、idempotency key、监督事件、verification 和 acceptance boundary；重启恢复不会
重复成功工具调用，失效授权不恢复执行，丢失结果时 fail-closed。

20 分钟 stall 协议通过缩短测试时钟作确定性故障注入：能检测 suspected stall、保留日志、
终止 owned process tree、最多一次等价重试且无残留进程。跨进程重启恢复正式演练结论为
`YUNOS_V10_F_RESTART_RECOVERY_GO`。

本机 Codex CLI/正式 Agent 接口审计结果为 `handoff_only`：`executor_available=false`，
`reason_code=codex_cli_access_denied`。系统保留标准 External Agent handoff artifact，但没有
逆向工程桌面窗口、UI 自动点击或伪造 Codex 执行成功；首版真实执行由原生 Agent 完成。

## 8. Verification、Acceptance 与学习边界

系统分别记录 `agent_reported`、`tool_executed`、`system_verified` 与 `user_accepted`。
工具返回成功和 Agent 自称完成都不会更新为用户已接受；只有 Acceptance Policy 满足或
明确 Acceptance Event 后，v9 当前状态才更新。

一次任务只能生成 Fact Record、Observation、Pattern/Workflow Candidate；不会直接修改
长期画像、稳定偏好或永久禁用规则。长期记忆候选仍遵循 v7 的批准和多次证据边界。

## 9. 数据库与生产迁移

v10 没有创建第六个数据库。schema generation `10001` 以加法式 migration 写入既有
Operational State DB；长期记忆继续由 v7 数据库提供，执行账本继续复用 v8 Runtime DB，
Objective、Plan、Run、Authorization、Proposal、Supervision、Verification 和 Learning
Candidate 使用 Operational State DB，并复用 v9 的 Delegation、Outcome、Approval、
Source Anchor、Conflict、Writer Lease 与 State Event。

生产迁移前完成五库/资产备份、服务静止、lease 0、克隆 migration、failure injection、
rollback、restore 与 v9 回归。迁移后 schema marker/digest、integrity、FK、events、projections、
writer off、`real-readonly`、Tailnet GET-only、Funnel off 与旧功能回归全部通过。

最终生产数据库字节级基线：

| 数据库 | Size | SHA-256 |
| --- | ---: | --- |
| Main | 76,333,056 | `0000000000000000000000000000000000000000000000000000000000000000` |
| Profile | 266,240 | `0000000000000000000000000000000000000000000000000000000000000000` |
| Semantic | 40,960 | `0000000000000000000000000000000000000000000000000000000000000000` |
| Runtime | 425,984 | `0000000000000000000000000000000000000000000000000000000000000000` |
| Operational | 1,474,560 | `0000000000000000000000000000000000000000000000000000000000000000` |

Main 与 Operational WAL 为 0 字节，SHM 均为 32,768 字节；writer lease 为 0。

## 10. 产品界面与写入边界

极简首页与 Omnibox 的“搜索 / AI”双模式保持不变，不增加独立 Agent 首页。AI 任务进入
简体中文“认知任务工作区”，按文档顺序显示目标、成功标准、明确不做、使用的上下文、计划、
决策、执行、验证、结果和学习候选；Prepare / Act 才显示右侧监督栏。

桌面是主要控制台。移动端只能查看目标、计划、进度、Agent、越权、验证与等待接受，不能
创建任务、授权、扩大权限、执行写工具、启动 Codex、接受高风险结果或 merge/tag/push。
普通 Web 与 Tailnet 保持只读；本版本没有把 Tailnet 反向代理的 loopback 来源当成本地写授权。

真实桌面/移动浏览器验收无控制台错误、无水平溢出、无写控件；PWA、Service Worker、
offline shell、GET-only 与 `no-store` 均无回归。

## 11. 最终回归

权威 v9.0.1 起跑门禁：1,495 collected、1,489 passed、6 skipped、0 failed、0 errors，
220.252 秒；五库和环境边界未变化。

最终代码树在 `3de64064e33436d11155d8949168c00a4480c65e` 完成一次从零 discovery：

- command：`.venv\\Scripts\\python.exe -B -m unittest discover -s tests -p test_*.py -v`；
- collected 2,198；passed 2,188；skipped 10；failed 0；errors 0；
- unittest 861.678 秒；launcher wall 862.764 秒；
- stdout SHA-256：`0000000000000000000000000000000000000000000000000000000000000000`；
- stderr SHA-256：`0000000000000000000000000000000000000000000000000000000000000000`；
- summary SHA-256：`0000000000000000000000000000000000000000000000000000000000000000`。

10 个 skip 均为已说明环境/平台契约：6 个 Windows symlink capability、1 个 POSIX FIFO、
2 个 POSIX process-group、1 个已存在生产 Operational DB 时禁止初始化；不存在失败或 error。

## 12. 恢复包与独立恢复契约

正式恢复包已经生成在 `output/v10-f-final-release-recovery-package-v2`，包含五库一致性备份、
sidecar、874 个资产、无秘密 Provider 配置模板、schema/release manifest、逻辑摘要、hash、
9 份节点/接纳报告与 recovery guide。正式结果为：

- 文件数 898、总字节数 619,770,776；
- `release_manifest.json` SHA-256：
  `0000000000000000000000000000000000000000000000000000000000000000`；
- manifest digest：
  `0000000000000000000000000000000000000000000000000000000000000000`；
- backup-set digest：
  `0000000000000000000000000000000000000000000000000000000000000000`；
- 首次 verify 与接纳后的 re-verify 均为 `verified`，verify report digest：
  `0000000000000000000000000000000000000000000000000000000000000000`；
- 五库逐库 file/logical/schema/integrity/FK、53 个 projection 重建与 snapshot 均匹配；
- 资产 874 个、540,711,854 字节，Provider secret material/reference 均为 false。

正式包随后恢复到独立路径和端口 18780。只读 Web 验收状态为 `accepted`，evidence SHA-256 为
`0000000000000000000000000000000000000000000000000000000000000000`，evidence digest 为
`0000000000000000000000000000000000000000000000000000000000000000`。恢复实例没有连接生产
数据库，Objective/Plan/Run、Now Package、Resume Package、Search 与桌面/移动只读工作区均与
正式包或生产 canonical payload 一致；四个写端点全部返回 403，Provider 调用为 0，恢复库
前后不变。launcher PID 与 service PID 虽因 Windows venv redirector 不同，但由同一 Job
Object 所有；终止后 active process 为 0、Job closed、端口无残留。

验收证据已复制到 `output/v10-f-final-release-seal`；可重建的独立 restore 工作目录随后按
ownership marker 精确清理，正式恢复包继续保留。恢复包内的 9 份 `version_reports` 是有意
冻结的 pre-package snapshot，其中 NEXT GATE 表述记录生成包之前的状态；本发布文档记录包、
restore、清理与恢复生产服务之后的 post-package 事实，避免为更新包内报告而制造自引用循环。

恢复后生产服务的最终动态边界另由
`output/v10-f-final-release-seal/final-production-live-boundary.json` 记录：单一 loopback listener
PID 与 `/api/status` PID 一致，loopback/Tailnet GET 均为 200 和 `no-store`，两侧共 8 个 POST
全部 403，Tailscale backend Running、Funnel false；五库哈希/sidecar 与 Runtime `6/6/6/12/30/0/6`
账本计数前后完全相同，Provider 调用增量为 0。evidence digest 为
`0000000000000000000000000000000000000000000000000000000000000000`。

## 13. 威胁模型结论

Threat Model 覆盖 prompt/tool-output injection、伪造授权/结果、计划扩张、秘密请求、路径逃逸、
symlink/junction、任意 shell/命令注入、重放/重复调用、授权过期、预算失控、无限循环、stall、
进程残留、网络外传、Provider/structured-output 故障、上下文过量/过期、错误 acceptance、长期
画像误写、移动/Tailnet/CSRF、schema/projection/backup/restore corruption 和自行创建下一版本。

每项均具备预防、检测、恢复、fail-closed 与测试。三轮架构、安全、产品复核没有未解决的
P0/P1/P2；Provider 不是安全边界，YunOS policy/authority/tool gateway 才是执行边界。

## 14. 已知限制

1. Kimi 没有当前凭据，只完成兼容适配器与契约测试。
2. Codex CLI 当前不可作为受支持的直接 executor，仅提供诚实的 handoff-only adapter。
3. Windows 当前环境不能创建部分 symlink；对应路径仍有拒绝逻辑和平台可用时测试。
4. POSIX FIFO/process-group 测试在 Windows 环境按平台契约跳过。
5. 生产系统保持 `real-readonly`；v10 Agent 的写能力只在独立、短期、逐任务授权的本地边界内开放。

这些限制不削弱原生受控 Agent 的真实执行闭环，也没有被表示成已具备的外部能力。

## 15. 明确未执行

本版本未执行 Historical Reconstruction、v8302、`preserved_out_of_scope_engineering` 激活、
v10-G、v10.1、v11、远端 push、stash 操作、移动端写入、Tailnet 写入、无人监督持续 Agent、
自动人生规划、自动修改长期人格、隐藏模型切换或生产 Agent 无授权 merge/tag/push。

最终本地 tag 名为 `v10.0-bounded-cognitive-agency`，message 为
`YunOS v10.0 — Bounded Cognitive Agency`。实际 tag object、peeled target、旧 tag 不变量、
stash/origin 不变量与 `push=false` 仅在全部恢复与静态门禁完成后生成并记录。
