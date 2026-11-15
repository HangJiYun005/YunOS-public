# YunOS v9.0.1 — 简体中文界面

## 1. 版本定位

YunOS v9.0.1 为现有 v8.1 桌面界面、v8.2 移动/PWA 与 v9
Operational State 建立统一的轻量级本地化基础，并将当前用户可见界面默认语言设为
简体中文。

本版本只改变展示语言，不改变系统语义。数据库枚举、API 字段、状态机值、事件类型、
审计标识、Runtime 行为、Delegation 权限和只读生产边界均保持原样。

正式语言策略：

- 默认语言：`zh-CN`；
- 回退语言：`en`；
- 中文译文存在时显示中文；
- 中文译文缺失但英文基准存在时显示英文；
- 两级字典均缺失时返回调用方提供的原始文本或安全占位；
- 不依赖浏览器语言将默认界面自动切回英文；
- 本版本不增加可见语言切换器，也不引入运行时机器翻译。

## 2. 本地化架构

本版本继续使用原生 HTML、CSS 与 Vanilla JavaScript，不增加依赖。

| 文件 | 职责 |
| --- | --- |
| `web/i18n/en.js`、`zh-CN.js` | 通用英文基准与简体中文主字典 |
| `web/i18n/*-shell-mobile-extra.js` | 应用壳、移动端与 PWA 补充词典 |
| `web/i18n/*-legacy-core.js` | v8.1 核心桌面、搜索、AI 与 Sidebar 词典 |
| `web/i18n/*-legacy-tools.js` | Runtime、Profile 与 Context 工具词典 |
| `web/i18n/*-operational-extra.js` | v9 Operational State 补充词典 |
| `web/i18n/index.js` | locale 解析、回退、插值、DOM 应用、枚举展示和 `Intl` 格式化 |
| `web/index.html` | `lang="zh-CN"`、静态文案 key、翻译脚本加载顺序 |
| `web/app.js` | 动态文案、状态映射、错误、ARIA、日期时间和数量展示 |
| `web/pwa.js` | PWA 安装与更新状态的本地化展示 |
| `web/offline-shell.html` | 最小离线壳的中文标题、说明和可访问名称 |

字典 key 按产品领域组织，例如 `common.*`、`nav.*`、`status.*`、
`project.*`、`delegation.*`、`offline.*`、`aria.*` 和 `error.*`，不使用页面序号
或完整中文句子作为 key。

公开的本地化接口包括：

- `t(key, params, original)`；
- `setLocale(locale)` 与 `getLocale()`；
- `applyDocument(root)`；
- `enumLabel(group, value, context)`；
- 完整日期、短日期、日期时间、相对时间、数字、计数、文件大小和时长格式化函数。

参数插值只生成纯文本，不使用 `eval`，不解析翻译内的 HTML。DOM 静态翻译通过
`textContent` 和受限属性白名单写入；事件属性、样式、URL 与 HTML 注入属性不会由
翻译层更新。参数缺失时使用空字符串安全降级，不会把 `undefined` 写入界面。

封板盘点为英文与简体中文各 `1,244` 个 key，单边 key、重复 key、跨模块碰撞和
占位符差异均为 0。`app.js` 中 850 个可静态识别的 `t()` key 均在两种语言中存在。

## 3. 汉化范围

### 全局应用壳

- 页面标题与 `lang="zh-CN"`；
- 一级导航、菜单、返回、关闭、展开、收起、重试、加载与空状态；
- tooltip、ARIA label、live region、屏幕阅读器提示和按钮可访问名称；
- 用户可理解的中文错误摘要与保持原样的技术错误详情。

### v8.1 桌面界面

- Omnibox、Search / AI 切换和 Memory Search；
- 搜索结果、AI 文档结果、来源、相关记忆、不确定性和执行详情；
- Contextual Sidebar、系统工具、空状态与错误状态；
- Profile、Prompt Inspector、Optimizer 与 Runtime 只读审阅中的用户可见文案。

`AI`、`Runtime`、Provider 名称和技术证据值继续保持既有技术名称。

### v8.2 移动端与 PWA

- 移动首页、搜索、排序、最近记录和加载更多；
- 结果计数、相关匹配、缓存范围、离线提示与缓存时间；
- 会话阅读器、双向消息加载、顶栏显示状态和连接错误；
- PWA 更新提示、Manifest 描述与离线应用壳。

聊天正文、用户消息、AI 原始回答和导入内容不会经过翻译层。

### v9 Operational State

- 当前、项目、等待、决策、委托五个一级入口；
- Now Package 与 Project Resume Package；
- 项目摘要、当前重点、阶段、权威基线、下一批准步骤与来源锚点；
- blocker、waiting、decision、delegation、outcome、conflict、freshness、
  candidate、approval、verification、overreach、stale 与 unverifiable 等展示映射；
- 时间线、空状态、加载状态、错误状态与可访问名称。

API 和数据库返回的英文枚举不被改写；仅在展示层映射为统一中文。

## 4. 日期、时间与数字

界面格式化使用浏览器标准能力：

- `Intl.DateTimeFormat("zh-CN", ...)`；
- `Intl.RelativeTimeFormat("zh-CN", ...)`；
- `Intl.NumberFormat("zh-CN", ...)`。

覆盖完整日期、短日期、日期时间、相对时间、文件大小、数量和运行/等待时长。
本地化只改变展示结果，不参与排序、比较、分页或状态计算。技术报告中的精确 ISO 8601
时间、原始字节数和摘要值可以保留。

## 5. 允许保留英文

允许继续显示的英文分为三类，并在 `web/i18n/index.js` 中形成显式 allowlist。

| 分类 | 允许内容 |
| --- | --- |
| 品牌与产品名（9） | YunOS、Ayun English Core、Codex、DeepSeek、Kimi、ChatGPT、Claude、Gemini、Ollama |
| 技术名称（42） | AI、Agent、Git、SQLite、Tailscale、PWA、API、Runtime、Provider、Markdown、TXT、Enter、SHA-256、commit、tag、branch、ISO 8601 等 |
| 原始或用户内容 | 项目名、用户消息、AI 回答、导入正文、代码、命令、日志、路径、哈希、事件内部名称、API 字段、数据库枚举 |

显式 allowlist 共 63 项：品牌 9 项、技术名称 42 项、原始或用户内容 12 项。上述内容
周围的解释性文案仍使用中文。允许列表不构成跳过翻译的通用豁免。

## 6. 中文排版与可访问性

本版本保留 YunOS 的极简视觉身份，仅针对中文显示进行局部约束：

- 按钮和可交互控件继续保持至少 44 CSS px 的触控高度；
- Operational State 状态栏允许在窄宽度下自然换行；
- 中文正文使用正常断行，路径、哈希和技术标识使用局部安全断词；
- 不对全局应用 `word-break: break-all`；
- 移动端 safe area、Visual Viewport、横屏与低高度布局继续沿用既有契约；
- 中文界面中的 ARIA、对话框名称、状态播报和键盘提示与可见文案保持一致。

最终桌面、移动、横屏、PWA 和水平溢出证据记录在版本关闭报告中。

## 7. Service Worker 与 PWA 边界

本版本的应用壳版本为：

```text
yunos-app-shell-v9-0-1-i18n-3
```

英文、简体中文和本地化运行时文件均进入应用壳。激活新版本时只清理旧的
`yunos-app-shell-*` 缓存，不触及私人内容缓存命名空间。

保持不变的安全边界：

- `/api/` 继续网络直通，不进入应用壳 CacheStorage；
- 私人内容缓存边界不扩大；
- API 继续 `no-store`；
- 移动产品操作继续 GET-only；
- Service Worker 更新不在用户阅读过程中强制刷新；
- 离线导航回退到已缓存应用壳，在线恢复后继续使用网络；
- Manifest 的 `name` 与 `short_name` 保持 `YunOS`，避免主屏幕名称被错误截断。

## 8. 数据库与生产边界

本版本不包含数据库迁移或业务数据写入。Main、Profile、Semantic、Runtime 与
Operational State 五个生产数据库及其 WAL/SHM sidecar 只允许以只读方式核验。

发布门禁要求封板前后同时证明：

- 五库大小、SHA-256、schema 与逻辑摘要保持一致；
- `PRAGMA integrity_check` 为 `ok`，foreign key violation 为 0；
- WAL 未新增业务写入；
- Operational State event、projection 与 snapshot 未变化；
- writer lease 仍为 0；
- 生产服务恢复为 `real-readonly`；
- Tailnet 私有 HTTPS 正常，Funnel 关闭。

精确封板数值只记录在关闭报告中，避免在产品说明中复制易漂移证据。

## 9. 已知限制

1. 本版本只交付内置 `zh-CN` 与 `en`，不提供语言包下载、在线翻译或复杂设置页。
2. 用户生成内容、导入聊天正文、日志和 AI 原始输出不会自动翻译。
3. 新增 key 若暂时缺少中文，会按正式回退链显示英文；这属于安全降级，不应显示 key。
4. 本版本在 Windows Chrome 中完成当前代码的浏览器、standalone、Service Worker、
   离线与在线恢复验收；Safari/iPhone 平台行为沿用 v8.2 已封存真机证据。本次没有
   Mac Web Inspector 复验，Windows Chromium 结果不会被改写成当前 Safari 真机执行。
5. 本版本不重写历史 Markdown 文档；历史发布记录中的英文不属于当前可达界面遗漏。

## 10. 明确非目标

本版本没有开始或顺带实现：

- 数据库 schema、业务内容或 API 语义修改；
- Runtime、状态模型或 Delegation 权限修改；
- writer、移动端写入或公开访问；
- 新 AI 功能、模型路由或 Provider 切换器；
- v9-G、v9.1 功能节点或 v10；
- 主题系统、设置中心、在线语言包或机器翻译。

最终 Git、测试、浏览器、数据库和生产状态证据见：

`docs/version_reports/yunos_v9_0_1_localization_closure.md`
