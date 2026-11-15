# YunOS 当前架构

YunOS 是本地优先的长期记忆、可信运行、当前状态与受限认知代理系统。v10 的权威详细设计见
[`design/yunos_v10_bounded_cognitive_agency_baseline.md`](design/yunos_v10_bounded_cognitive_agency_baseline.md)，
本文只保留当前架构索引，不复制专题契约。

## 分层架构

| 层 | 职责 |
|---|---|
| v7 Memory and Evidence | Memory Package、Evidence Selection、Temporal Memory 与最小必要上下文来源 |
| v8 Trusted Runtime | execution、receipt、usage、artifact、ledger、failure state 与 exactly-once |
| v9 Operational State | Now Package、Project Resume、Delegation、Outcome、Freshness 与当前状态 |
| v10 Bounded Cognitive Agency | Objective、Plan、Provider、Capability、Authorization、Tool Gateway、Supervision、Verification、Acceptance 与 Learning Candidate |

v10 只在 v7/v8/v9 之上增加认知代理层，不创建第二套 Evidence、Runtime、Operational State、
Delegation、Outcome 或 Approval 系统。

## Search 与 AI 分离

Search 模式沿用本地 SQLite/FTS 与 Operational State 查询链：不调用模型、不产生 API 费用、
不向外发送内容，并继续支持移动端与离线缓存。

AI 模式是独立链路。它先建立 Objective Contract，再从 v7 Memory Package 与 v9 Now/Resume
Package 装配有来源、有预算的最小必要上下文。Provider 必须由用户手动选择；模型输出只能是
回答、计划或 typed tool proposal，不能直接获得文件系统、Git、数据库、网络或凭据权限。

## 五库复用

| 数据库 | v10 中的职责 |
|---|---|
| Main DB | 保持历史内容与搜索来源 |
| Profile DB | 保持已批准画像和既有控制边界 |
| Semantic DB | 保持语义索引与检索数据 |
| Runtime DB | 记录真实 Provider 和工具执行的 receipt、usage、artifact 与 ledger |
| Operational State DB | 以加法式 schema 保存 Objective、Plan、Cognitive Run、Authorization 引用及监督状态 |

跨库只保存稳定引用和 digest，不建立跨数据库 Foreign Key 假设，不创建第六个数据库。

## 执行与访问边界

模型只提出 capability 和 typed arguments。YunOS Tool Gateway 负责 schema、Objective、Plan、
Authorization、风险、路径、命令、秘密过滤、receipt 与 verification 检查；工具成功不等于
系统验证，系统验证也不等于用户接受。

普通 Web、Tailnet 与移动端保持只读。移动端不能创建任务、授权执行、接受高风险结果或调用
写工具。Act 写入只能经过本机专用 local broker/CLI 或能够证明不暴露给 Tailnet 的本机管理
边界；不能仅凭 remote IP、Origin、User-Agent 或隐藏按钮判断本机权限。

## 发布边界

生产默认 `real-readonly`，writer lease 为零。当前路线止于 v10-F；不创建 v10-G、v10.1 或
v11，不激活 Historical Reconstruction 或 `v8302`。权威版本状态见
[`design/yunos_authoritative_roadmap.md`](design/yunos_authoritative_roadmap.md)。
