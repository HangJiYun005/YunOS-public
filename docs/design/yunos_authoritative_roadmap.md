# YunOS 权威路线图

## 当前权威版本路线

| 版本 | 权威主题 | 当前状态 |
|---|---|---|
| v9.0.1 | Simplified Chinese Interface | v10 的权威起跑基线；既有 commit 与 annotated tag 保持不变 |
| v10.0 | Agency Design Baseline | 已关闭；定义 Bounded Cognitive Agency 的架构、安全、产品和迁移基线 |
| v10-A | Objective and Intent Engine | 已关闭；实现 Objective Contract、Agency Level 与 Cognitive Stance |
| v10-B | Planning and Capability Engine | 已关闭；实现 Plan Graph 与 Capability Registry |
| v10-C | Provider and Cognitive Orchestration | 已关闭；实现 Provider Registry、最小上下文和 Runtime bridge |
| v10-D | Bounded Execution and Supervision | 已关闭；实现 Authorization、Tool Gateway、受控 Agent Loop 与恢复边界 |
| v10-E | Agency Workspace and Feedback | 已关闭；实现简体中文认知任务工作区和移动端只读查看 |
| v10-F | Production Adoption and Final Seal | 当前且唯一的封板节点；完成生产接纳、本地 `main`、annotated tag 与最终报告后停止 |

## 路线裁决

当前正式路线只有：

`v9.0.1 -> v10.0 -> v10-A -> v10-B -> v10-C -> v10-D -> v10-E -> v10-F`

v10-F 完成后立即停止。本路线不创建或预留 v10-G、v10.1、v11，也不把延期项升级成新的
版本节点。任何后续版本都必须由新的、独立的用户授权建立，不能由 Agent 自行生成。

## 架构继承

- v7 提供 Memory Package、Evidence Selection 与 Temporal Memory；
- v8 提供 Trusted Runtime、receipt、ledger、exactly-once 与受控执行基础；
- v9 提供 Operational State、Now/Resume Package、Delegation 与 Outcome；
- v10 在上述权威层之上提供目标理解、计划、能力选择、授权绑定、受控执行、监督、验证、
  接受和学习候选，不复制前三层。

搜索模式保持完全本地，不调用模型。AI 模式使用手动选择的 Provider 和最小必要上下文，
模型只能提出工具调用，真正执行必须经过 YunOS 的 Authorization 与 Tool Gateway。

## 数据与访问边界

- 继续复用 Main、Profile、Semantic、Runtime、Operational State 五个 SQLite 数据库，不创建
  第六个数据库；
- Provider 调用继续进入 v8 Runtime 的 execution、receipt、usage、artifact 与 ledger；
- Objective、Plan、Cognitive Run 等 v10 状态进入 v9 Operational State DB 的加法式扩展；
- 生产默认 `real-readonly`，writer lease 为零；
- Tailnet 与移动端保持只读，受控写入只能通过不暴露给 Tailnet 的本机边界执行。

## 冻结的历史路线

Historical Reconstruction、historical backfill、`v8302` 与
`preserved_out_of_scope_engineering` 均不属于当前 v10 路线，不得激活、应用或恢复推进。
旧文档、旧 schema 和旧 tag 仅作为历史证据保留，不改写 Git 历史，不修改旧 tag，不借 v10
封板重新开启旧工程路线。

## 发布治理边界

- v10-F 以前的内部 `GO` 不等于生产接纳或用户接受；
- 只有最终全量、生产迁移、真实验收、恢复包、独立恢复和 Git 门禁全部通过，才能声明
  `YUNOS_V10_FINAL_SEAL_GO`；
- 本地发布只允许 `main --ff-only` 和指定 annotated tag；不 rebase、不 squash、不 force、
  不 push、不操作既有 stash；
- 最终 tag 为 `v10.0-bounded-cognitive-agency`。tag 完成后不自动进入下一版本。
