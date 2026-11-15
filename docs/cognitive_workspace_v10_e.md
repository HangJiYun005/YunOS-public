# YunOS v10-E — 认知任务工作区与只读 PWA 边界

## 1. 产品定位

v10-E 在原有极简首页的 `搜索 / AI` Omnibox 内升级 AI 任务体验，不新增独立 Agent 首页。
搜索继续直接读取本地 SQLite / FTS / Operational State，不装配模型上下文、不调用 Provider、
不产生模型 usage 或费用。AI 任务进入简体中文优先的“认知任务工作区”。

工作区是认知任务的可解释、只读监督视图，不是通用管理后台，也不是授权来源。模型输出、工具回执、
系统验证与用户接受保持四层分离；任何前端缓存都不能用于授权执行或宣告接受。

## 2. 稳定产品契约

桌面工作区根节点使用 `data-cognitive-workspace`，主文档各区使用
`data-cognitive-section`：

| marker | 简体中文标题 | 内容 |
| --- | --- | --- |
| `objective` | 目标理解 | Objective Contract 的解释目标与来源 |
| `success` | 成功标准 | 可验证验收条件 |
| `prohibited` | 明确不做 | 禁止结果与版本边界 |
| `context` | 使用的个人上下文 | 最小必要来源、预算和省略说明 |
| `plan` | 计划 | Plan Graph 节点、依赖和状态 |
| `decisions` | 决策点 | 歧义、人工门禁和 Plan Revision |
| `execution` | 当前执行 | proposal、tool execution 和最后活动 |
| `verification` | 验证 | 独立验证，而非 Agent 自报 |
| `outcome` | 结果 | 影响、回滚、当前判定和接受状态 |
| `learning` | 学习候选 | fact / observation / workflow candidate |

顶部同时显示任务标题、项目、状态、Provider、Agency Level、Cognitive Stance、风险和最后活动。
`prepare` / `act` 时展示 `data-cognitive-supervision` 监督栏，包括 Agent、读写根、允许/禁止能力、
Git/network/database 权限、预算、运行时间、stall 和 overreach。监督栏只展示已持久化事实，不能扩大
Authorization Artifact。

内部枚举保留英文；默认简体中文映射如下：

- `consult / plan / prepare / act` → `讨论 / 规划 / 准备 / 执行`；
- `continue / explore / breakthrough` → `延续 / 探索 / 突破`。

翻译缺失时由既有 `t(key, params, fallback)` 使用英文 fallback，不展示内部 key。

## 3. 移动端边界

移动认知视图使用 `data-cognitive-mobile-readonly`，只允许查看 Objective、Plan、进度、Agent、越权、
验证和等待接受。移动 UI 不提供下列 marker 或操作：

- `data-cognitive-mutation`；
- `data-cognitive-authorize`；
- `data-cognitive-accept`；
- `data-cognitive-write-tool`；
- `data-cognitive-start-codex`。

移动端不能创建任务、授权或扩大权限、接受高风险结果、调用写工具、启动 Codex、merge、tag 或 push。
安全边界不依赖隐藏按钮、User-Agent、remote IP 或单一 Origin；普通 Web/Tailnet 服务没有 cognitive
mutation route。无法证明本机写入口与 Tailnet 隔离时，写入继续只经 local broker/CLI。

## 4. PWA 与缓存

Service Worker 只缓存固定的静态 application shell allowlist：HTML、CSS、i18n、前端脚本、manifest
和图标。所有 `/api/*` 请求直接 `fetch(request)`，不读取或写入 CacheStorage，也没有 Background Sync
或 Periodic Sync。认知 GET 和其他私人 JSON 响应统一 `Cache-Control: no-store`，前端统一使用
`fetch(..., {cache: "no-store"})`。

因此：

- 离线搜索仅使用既有、本机、带 TTL 和覆盖范围标识的 IndexedDB 私人文本缓存；
- Objective、Plan、Run、Authorization、execution、verification 和 acceptance 不进入 PWA cache；
- 离线壳不能把旧认知状态标成实时状态；
- cached/offline 内容不能授予执行、接受结果或更新项目完成状态；
- Search 的离线能力不调用模型，也不复用 AI Runtime。

每次正式前端发布必须同时更新：

1. `web/service-worker.js` 的 `APP_SHELL_VERSION`；
2. Service Worker 静态 allowlist 的 `?v=`；
3. `web/index.html` 和 `web/offline-shell.html` 的静态资源 `?v=`。

三者不一致会导致离线壳请求不在 allowlist 中，必须由合同测试阻断。v10-E 正式版本固定为
`v10-e-agency-workspace-2`。本次冻结中 `style.css` 保持 `10-e-1`，发生竞态修复和文案变更的
`app.js` 与 shell i18n 资源使用 `10-e-2`。

### Runtime / Workspace 异步竞态

Runtime preview 与认知工作区是相互独立的只读请求。若 Runtime 先返回失败，而同一精确任务标题绑定的权威 Workspace 稍后返回，页面必须：

- 保留 Runtime failure 的 message、technical detail 和不自动重试边界；
- 使用晚到的权威 Objective、Plan、Agency Level、Cognitive Stance、Run 和 Authorization 重新渲染；
- 对 `prepare` / `act` 恢复监督栏；
- 不把 Workspace 成功读取改写成 Runtime 成功，也不抹除失败事实；
- 继续保持十个文档区段和独立 Acceptance 状态。

## 5. 响应式与可访问性

工作区使用内容驱动布局，不依赖像素级快照：网格子项 `min-width: 0`，长路径、digest、错误和工具输出
必须可换行或在受控容器内滚动；页面根不得出现横向溢出。桌面以主文档和监督栏为核心，窄屏收敛为
单列只读文档。触控目标、焦点轮廓、`aria-live`、标题层级和 `prefers-reduced-motion` 继承现有产品
规则。

## 6. 验收证据

静态/契约测试：`tests/test_v10_e_pwa_product.py`。

真实浏览器验收必须至少覆盖：

- Search 请求只命中本地 search API，Provider/Runtime/认知执行请求为零；
- 认知 GET 响应和 fetch 均为 `no-store`，Service Worker CacheStorage 无 `/api/`；
- 桌面工作区十个主文档区和 Prepare/Act 监督栏可见；
- 移动认知视图为只读，无写入/授权/接受/Codex 操作；
- 390 px 与桌面宽度均无水平溢出、无 console/page error；
- 默认简体中文，英文 fallback 不显示裸 i18n key；
- 主机离线后仅显示明确的离线/陈旧标识，不将认知缓存冒充实时数据。

所有浏览器证据写入 `output/playwright/`；该目录是验收 artifact，不进入 PWA precache。
