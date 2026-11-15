# YunOS v10 Tool Gateway

Tool Gateway 是模型建议与真实副作用之间的唯一执行边界。模型只能产生 typed Tool Proposal；
不能直接获得 shell、文件系统、Git、数据库、网络或凭据。

## 1. 执行链

每次 preview 与 execute 都按同一顺序重新验证：

1. input / output JSON schema；
2. Objective 与 Plan Node scope；
3. Capability Registry identity；
4. Authorization Artifact、expiry、risk 与预算；
5. canonical path、文件身份、reparse / hardlink / root containment；
6. typed argv 与 command template；
7. secret / injection / output-size policy；
8. sealed reservation 与 idempotency key；
9. local broker 执行；
10. Runtime / Operational receipt；
11. 独立 Verification；
12. checkpoint、state transition 与必要时 rollback。

Preview 是只读工件，不会创建副作用。Execute 必须与 preview 的 proposal、Node、authority、path、
command、budget 和 expected state 完全相同。

## 2. Typed capability

首版仅开放 Registry 中明确注册且 executor 可用的能力。文件变更使用结构化参数，例如：

- `project.file.patch`：`relative_path`、before/after SHA-256、replacement content；
- `project.change.rollback`：owned `backup_reference` 与当前文件 SHA-256；
- 固定只读 Git / test capability：预定义 argv，无 shell。

不接受任意 shell 字符串、PowerShell 片段、管道、重定向、命令替换、交互式命令、隐式网络、
`reset --hard`、clean、rebase、merge、tag、push 或跨授权根目录写入。

## 3. 路径与文件身份

Gateway 与 local broker 独立检查：

- `..`、absolute / UNC / device path、ADS 与 drive switching；
- case / prefix confusion；
- symlink、junction、mount/reparse escape；
- hardlink alias、TOCTOU 与替换后的文件身份；
- write root、owned temporary / backup root 与 project root；
- before / after digest 和最大输入输出尺寸。

路径字符串通过校验仍不够；执行前后都要重新读取真实文件身份和 digest。

## 4. Receipt、Verification 与 rollback

成功工具调用至少产生 proposal、reservation、execution、broker receipt、verification 和 checkpoint。
`project.file.patch` 返回 owned `backup_reference`；rollback 只能使用同一 run 拥有且经过重新验证的
备份记录。工具退出码为零不等于任务成功；Verification 必须独立验证 diff、文件 digest、测试或
业务验收条件。

Agent reported、Tool executed、System verified、User accepted 分别持久化。Acceptance Event 之前
结果保持 `waiting_acceptance` 或 policy 规定的未接受状态。

## 5. 越权与恢复

越权请求在副作用前拒绝，并写入 `overreach_blocked` supervision event；不得创建 tool execution，
不得更新项目完成状态。已持久化 run 在 crash 后从最后安全 checkpoint 恢复；成功 execution 不得
重复，失效授权不得恢复，丢失结果 fail-closed，rollback 只处理本 run owned change。

## 6. Web、Tailnet 与移动边界

普通 Web 与 Tailnet 保持 GET-only / no-store 认知查看面。桌面按钮不是安全边界；首版写入只经
本机 CLI / local broker，Tailnet Serve 不能访问独立写入口。移动端不创建任务、不授权、不接受
高风险结果、不启动 Agent，也没有写 capability。

## 7. 实现索引

- `memory/cognitive_tool_gateway.py`
- `memory/cognitive_capability.py`
- `memory/cognitive_execution_capabilities.py`
- `memory/cognitive_local_broker.py`
- `memory/cognitive_verification.py`
- `memory/cognitive_agent_loop.py`
- `tests/test_v10_tool_gateway.py`
- `tests/test_v10_path_toctou.py`
- `tests/test_v10_supervision_broker.py`
