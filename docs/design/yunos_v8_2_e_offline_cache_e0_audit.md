# YunOS v8.2-E Offline Cache E0 仓库审计与设计关闭

> - 节点：v8.2-E0
> - 状态：E0 仓库审计完成；E1-E5 尚未实现
> - 审计基线提交：`89ecb51689393b77e25feb5972dade075ba96634`
> - 审计分支：`feature/v8.2-d-tailscale-private-access`
> - 审计日期：2026-07-25
> - 产品基线：`YunOS v8.2-E 离线缓存设计基线 v0`
> - 仓库基线：`docs/design/yunos_v8_2_e_offline_cache_baseline.md`
> - 产品基线 SHA-256：`0000000000000000000000000000000000000000000000000000000000000000`

## 1. 结论

E0 仓库级审计结论为 **GO（仅限设计关闭）**。

这不表示 v8.2-E 功能已经可用，也不表示 E 节点可以验收。当前仓库没有私人内容
IndexedDB、离线正文回退、离线本地搜索、30 天 TTL、容量治理、分类 LRU、24 小时
状态恢复或媒体缓存。E1-E5 的任一生产能力均未在本节点实现。

仓库能够承接产品基线，但实施时必须遵守以下结论：

1. 保留现有 Service Worker 的应用壳命名空间和 `/api/*` 网络直通边界。
2. 私人正文、搜索快照、媒体、索引和阅读状态不得进入
   `yunos-app-shell-*` CacheStorage。
3. 私人内容使用独立 IndexedDB `yunos-private-content`；恢复状态使用独立
   IndexedDB `yunos-mobile-resume`。
4. 当前离线导航优先返回最小离线页，无法进入完整移动阅读界面。E2 在离线内核可用
   后必须让离线导航加载缓存的完整应用壳，并保留最小离线页作为最终兜底。
5. 当前搜索和会话 API 没有 `Cache-Control: no-store`。E2 必须同时在响应头和前端
   私人数据请求上禁止浏览器 HTTP 缓存，把离线副本唯一交给受控 IndexedDB 管理。
6. 当前没有缩略图、原图或附件的可用 GET API。E4 在建立不泄露本地路径的媒体契约
   前不得开始媒体缓存。
7. 真实 iPhone Safari/PWA 的配额、驱逐、大 Blob、后台暂停和安装更新行为只能在
   v8.2-F 真机验收，Windows Chromium 结果不能替代。

总体 E 节点仍为 **NO-GO**，直至 E1-E5 全部门禁通过。

## 2. 审计边界

本次只执行：

- 核对权威提交与当前分支；
- 读取 Service Worker、PWA 注册、移动搜索、会话阅读器和 API 实现；
- 只读核对生产库 schema、稳定 ID 完整性和附件元数据概况；
- 核对 HTTP 响应能力、测试入口和真机调试资料；
- 形成仓库适配设计和分阶段门禁。

本次没有：

- 修改 Web、API、数据库或运行时生产代码；
- 创建 IndexedDB；
- 写入、复制、迁移或压缩生产 SQLite；
- 下载依赖、模型或媒体；
- 启动 E1-E5；
- merge、tag、push 或发布；
- 声称完成 iPhone 真机验证。

审计开始时已有未跟踪目录 `.playwright-cli/`、`output/` 和
`tools/installers/`。这些目录不属于本次改动。

## 3. 当前仓库事实

### 3.1 应用壳与 Service Worker

当前实现位于：

- `web/service-worker.js`
- `web/pwa.js`
- `web/index.html`
- `web/offline-shell.html`

已满足的基础条件：

- 应用壳缓存前缀是 `yunos-app-shell-`；
- 当前应用壳版本是 `v8.2-d-2`；
- 预缓存是明确的静态 URL 白名单；
- `/api/*` 在 fetch 事件中直接 `fetch(request)`，不读写 CacheStorage；
- 非 GET 请求不由 Service Worker 处理；
- 激活时只删除同前缀的旧应用壳缓存；
- `skipWaiting()` 只响应显式 `YUNOS_ACTIVATE_UPDATE`；
- 普通激活逻辑不会删除任何 IndexedDB。

当前限制：

- 导航失败时优先返回 `offline-shell.html`，然后才尝试 `index.html`；
- `offline-shell.html` 是最小错误页，不加载完整移动端 `app.js`；
- 因此当前架构只能显示“主机不可用”，不能从未来的私人 IndexedDB 进入离线阅读。

E2 的应用壳切换必须满足：

1. `index.html`、离线数据访问模块和本地搜索 Worker 已作为静态壳完整预缓存；
2. 完整应用壳在无主机时不会无条件执行桌面数据请求；
3. 导航网络失败后返回缓存的 `index.html`；
4. `offline-shell.html` 仅在完整壳缺失、损坏或初始化失败时兜底；
5. 应用壳更新仍只清理 `yunos-app-shell-*`；
6. 私人数据库从不由 Service Worker 打开、迁移或删除。

### 3.2 前端请求封装

当前统一请求函数是 `web/app.js` 中的 `api(path, options)`。

现状：

- 默认使用相对同源 URL；
- 移动搜索、附近消息预览和会话阅读均使用 GET；
- `AbortError` 与普通网络错误分开；
- fetch 抛错时发出 `yunos:host-connectivity` 不可用事件；
- 收到 HTTP 响应后先标记主机可用，再解析 JSON 和判断 `response.ok`；
- 没有统一超时；
- 私人 GET 没有统一 `cache: "no-store"`；
- HTTP 错误、网络错误、超时和用户主动取消尚未形成离线回退状态机。

E2 必须在应用数据访问层实现明确分类：

| 在线结果 | 行为 |
|---|---|
| 2xx 且完整 JSON 契约有效 | 显示在线数据；异步写入白名单字段；更新 `validatedAt` |
| 用户主动取消或新请求替换旧请求 | 不回退旧副本，不改变校验时间 |
| 网络异常或内部超时 | 查找仍有效副本；存在时明确显示离线状态，不存在时显示“此内容未缓存” |
| 5xx 或临时不可用 | 可以回退有效副本，但必须显示主机响应失败和副本时间 |
| 4xx、游标冲突或契约错误 | 不自动伪装为正常离线结果；如提供本地副本，只能作为明确的手动旧副本入口 |
| 过期或损坏副本 | 不进入普通离线结果；显示过期、缺失或已被系统清理 |

不能只依赖 `navigator.onLine` 决定数据状态。该值只可作为提示信号，真实请求结果才是
在线状态依据。

### 3.3 搜索 API 与稳定 ID

移动搜索使用：

```text
GET /api/search
  ?q=...
  &scope=all
  &aggregate=conversation
  &sort=relevance|latest
  &offset=...
  &limit=20
```

搜索项当前可提供：

- `conversation_id`
- `message_id`
- `title`
- `role`
- `snippet`
- `update_time_iso`
- `source_platform`
- `memory_space`
- `reference_uri`
- `status`
- `tags`

分组结果还包含 `best_hit`、`other_hits`、总数和分页字段。

只读生产库核验结果：

| 项目 | 数量 |
|---|---:|
| 会话 | 409 |
| 消息 | 16,324 |
| 空 `conversation_id` | 0 |
| 重复 `conversation_id` 组 | 0 |
| 空 `message_id` | 0 |
| 重复 `(conversation_id, message_id)` 组 | 0 |
| 跨会话复用 `message_id` 组 | 0 |

结论：

- 当前生产快照可以使用 `(conversation_id, message_id)` 作为正文缓存稳定主键；
- 当前 API 已按复合锚点定位消息；
- 不得用标题、ordinal、时间或正文哈希替代主键；
- 当前服务端没有独立内容修订号。E1 应对在线白名单正文计算本地
  `contentVersion`，在线成功后覆盖同键记录；
- 未来导入器若改变 ID 语义，必须触发兼容性审计，不能静默复用旧缓存。

### 3.4 会话分段与游标

移动阅读使用：

```text
GET /api/conversation-messages
```

初始请求使用 `conversation_id + anchor_message_id`，向前和向后分页使用带校验的游标。
服务端以 SQLite `mode=ro` 打开数据库，并在游标中绑定：

- `conversation_id`
- 焦点 `message_id`
- 边界 `message_id`
- 有序位置
- rowid
- 当前总消息数
- 校验和

正文响应包含消息稳定 ID、角色、时间、文本、位置和追溯字段。

E2 缓存只允许保存阅读所需的最小白名单：

- `conversationId`
- `messageId`
- `role`
- `text`
- `messageTime`
- 必要的显示顺序
- 本地 `contentVersion`
- `cachedAt`
- `validatedAt`
- `expiresAt`
- `lastAccessedAt`
- `byteSize`

不得整体缓存 API 响应。以下字段默认不落入离线正文：

- `metadata_json`
- `model_trace`
- `model_slug`
- `default_model_slug`
- 原始 `reference_uri`
- 服务端路径、host、数据库路径或运行时字段
- 桌面标记、备注、标签和管理状态

会话分段只保存消息引用和边界。游标属于当前在线分页状态，可能因主机数据变化而失效，
不能作为长期离线稳定标识。

### 3.5 当前移动状态

当前状态分为三类：

1. 搜索、分页、展开项和阅读状态主要保存在页面内存；
2. `history.pushState/replaceState` 保存当前浏览历史入口和滚动引用；
3. `localStorage["yunos.mobileSearch.history.v1"]` 保存最近搜索和最近会话，最多 12 条。

阅读正文快照当前只在 `Map` 中：

- 消息数组；
- before/after 游标；
- 结果页和阅读页滚动位置；
- 展开的追溯项；
- 顶栏显隐。

`pagehide` 只把状态复制到内存 Map，不持久化。刷新、PWA 被系统终止或重新启动后，
这些正文快照不可恢复。启动流程也不读取 URL 中的移动会话参数来恢复离线状态。

E3 决策：

- 使用独立数据库 `yunos-mobile-resume`；
- 恢复记录必须带 `savedAt` 和 `resumeStateVersion`；
- `savedAt` 超过 24 小时、结构不兼容、引用缺失、迁移中或损坏时回到移动首页；
- 现有 localStorage 不能继续作为正文或完整恢复状态来源；
- 如保留“最近访问”体验，只迁移经过 schema 校验的最小引用和时间，不自动把历史记录
  升级为离线正文；
- 一次性迁移成功或明确放弃后删除旧 localStorage 键；
- “清空离线内容”同时清除私人正文、媒体、索引、统计、恢复状态和旧键。

### 3.6 附件、缩略图和原图

生产库存在 `assets` 表，共 172 条，字段只有：

- `asset_file`
- `original_name`
- `size_bytes`

该表没有：

- `conversation_id`
- `message_id`
- 稳定 `attachment_id`
- MIME
- ETag
- Last-Modified
- 缩略图/原图关系
- 完整性摘要
- 可供浏览器访问的同源 URL

消息元数据中存在附件、文件 ID、缩略图和本地路径信号，但当前移动 API 不返回可用媒体
契约。仓库也没有附件、缩略图或原图 GET 路由。

当前 HTTP 行为：

- JSON API 只显式返回 `Content-Type` 和 `Content-Length`；
- 静态文件处理器返回 `Content-Length` 和 `Last-Modified`；
- 当前 Python 静态处理器不实现 ETag 或 Range；
- 私人资产目录不在 Web 根目录，且不得为了 E4 直接暴露。

E4 的硬前置条件：

1. 建立从消息到附件的可靠关联；
2. 对外只使用不可推导本地路径的稳定 opaque ID；
3. 新增明确的只读同源 GET 路由；
4. 路由不得接受任意文件路径；
5. 响应至少定义 `Content-Type`、`Content-Length`、ETag 或等价完整性标识；
6. 明确 Last-Modified 语义；
7. 明确 Range 支持策略；初版不得缓存零散 Range 片段；
8. 定义缩略图与原图的独立身份；
9. 只有缩略图成功展示后才能缓存缩略图；
10. 原图或附件只有用户主动打开且完整响应校验通过后才能缓存。

在以上契约完成前，E4 为 **BLOCKED**。不得从 `metadata_json`、正文中的
`sandbox:/mnt/data` 或 `asset_file` 拼接本地路径。

### 3.7 HTTP 缓存边界

Service Worker 已保证 `/api/*` 不进入 CacheStorage，但当前 JSON 响应没有
`Cache-Control: no-store`，多数前端私人 GET 也未设置 `cache: "no-store"`。

E2 必须同时执行：

- 服务端私人 JSON/媒体元数据响应发送 `Cache-Control: no-store`；
- 前端在线私人内容请求设置 `cache: "no-store"`；
- Service Worker 继续对 `/api/*` 直接联网；
- 离线副本只由应用数据访问层写入 IndexedDB；
- 不使用 Service Worker 的 stale-while-revalidate、cache-first 或网络失败静默旧响应。

这能避免受控私人缓存之外又出现不可统计、不可解释、不可按产品入口清理的浏览器 HTTP
缓存副本。

### 3.8 GET-only 与生产数据库

当前移动搜索、预览和阅读代码没有移动端 POST。服务端虽然保留桌面管理 POST 路由，
但 `web-real-readonly` 会拒绝未授权写入，移动端不调用这些路由。

E1-E5 必须保持：

- 移动业务网络请求只有 GET；
- 不注册 Background Sync 写回；
- 不创建上传、编辑、标注、删除或同步队列；
- 浏览器本地 IndexedDB 写入不改变服务端只读边界；
- 生产 SQLite、WAL 和 SHM 不复制到手机，也不因验收发生变化；
- 不缓存 AI 上下文、Runtime、模型响应缓存或认证信息。

### 3.9 当前测试和真机调试资料

当前相关自动化入口包括：

- `tests/test_v82_pwa_foundation.py`
- `tests/test_v82_mobile_search_api.py`
- `tests/test_v82_mobile_home_search.py`
- `tests/test_v82_mobile_reader_api.py`
- `tests/test_v82_mobile_preview_reader.py`
- `tests/browser/v82_mobile_home_search.js`
- `tests/browser/v82_mobile_preview_reader.js`

E0 聚焦回归共 38 项，通过；`web/app.js`、`web/pwa.js` 和
`web/service-worker.js` 通过 Node 语法检查。

当前测试中 `test_v82_mobile_home_search.py` 明确禁止 v8.2-B 移动控制器直接使用
`indexedDB` 和 `caches.open`。E1 应采用独立离线模块，并增加 E 节点专属契约测试；
不得简单删除旧测试所保护的“B 节点不自行缓存”和“Service Worker 不缓存 API”语义。

仓库当前没有可执行的 iPhone Safari Web Inspector 调试说明，也没有 Safari 自动化
入口。E5/F 前必须补充：

- 真机安装与更新步骤；
- 查看 IndexedDB、CacheStorage 和配额的方法；
- 模拟主机离线、Tailnet 不可达和系统清理的步骤；
- 收集 console、network、storage 和截图证据的方法；
- 无 Mac/Safari 调试能力时的明确限制和人工记录模板。

## 4. 仓库适配后的目标结构

### 4.1 应用壳

```text
CacheStorage
└── yunos-app-shell-v8.2-e-*
    ├── index.html
    ├── offline-shell.html
    ├── style.css
    ├── pwa.js
    ├── app.js
    ├── offline-cache.js
    ├── mobile-resume.js
    ├── offline-search-worker.js
    ├── manifest
    └── icons
```

这些文件是代码和静态资源，不含私人正文。

### 4.2 私人内容

```text
IndexedDB: yunos-private-content
├── meta
├── conversations
├── messages
├── segments
├── searchSnapshots
├── thumbnails
└── mediaObjects
```

建议版本：

- IndexedDB version：`1`
- `contentSchemaVersion`：独立字符串，首版由 E1 固定；
- 应用壳版本不得决定私人内容清理；
- 所有 store 的记录必须可从 schema 白名单重建，不能保存原始 API 对象。

关键主键：

- `messages`：`[conversationId, messageId]`
- `segments`：`segmentKey`
- `searchSnapshots`：`queryHash + sortMode`
- `thumbnails`：`mediaKey`
- `mediaObjects`：`mediaKey`

必要索引：

- `conversationId`
- `expiresAt`
- `lastAccessedAt`
- 内容类别

### 4.3 恢复状态

```text
IndexedDB: yunos-mobile-resume
└── resumeStates
```

只保存恢复所需的查询、排序、引用、展开状态、滚动位置、路由、顶栏状态、`savedAt` 和
`resumeStateVersion`。正文继续由 `yunos-private-content` 提供。

### 4.4 模块边界

建议新增独立静态模块：

- `web/offline-cache.js`：schema、事务、TTL、配额、逻辑字节统计、LRU、自愈和清空；
- `web/mobile-resume.js`：24 小时恢复数据库和兼容性校验；
- `web/offline-search-worker.js`：只读扫描已缓存正文，支持分批结果和取消；
- `web/app.js`：只负责在线/离线选择、UI 状态和当前会话保护集；
- `web/service-worker.js`：只负责应用壳，不打开私人数据库。

E1 不应把 IndexedDB 内核继续堆入当前约 269 KiB 的 `web/app.js`。

## 5. 时间、容量和清理决策

### 5.1 时间

每条内容记录：

- `cachedAt`
- `validatedAt`
- `expiresAt = validatedAt + 30 days`
- `lastAccessedAt`

本地读取只更新 `lastAccessedAt`。只有完整、契约有效的在线响应可以更新
`validatedAt` 和 `expiresAt`。

meta 保存最近可信墙钟时间。发现设备时钟回拨时：

- 剩余 TTL 不得增加；
- 使用不小于最近可信时间的保守时间判断；
- 标记时钟异常；
- 无法可靠判断时不自动展示普通有效副本，要求联网校验。

### 5.2 容量

```text
effectiveTarget  = min(1 GiB, quota × 50%)
effectiveCeiling = min(2 GiB, quota × 70%)
```

estimate 不可用时：

```text
effectiveTarget  = 256 MiB
effectiveCeiling = 512 MiB
```

1 GiB 是目标，2 GiB 是 YunOS 自身逻辑上限，均不是设备保证。

逻辑字节统计和 `navigator.storage.estimate()` 必须分别记录。逻辑统计用于分类清理，
estimate 用于 origin 整体压力判断；两者不一致时触发修复，不把估算值当精确账本。

### 5.3 QuotaExceededError

写入流程：

1. 预测写入后是否超过 `effectiveCeiling`；
2. 超限时先清理，不开始写入；
3. 捕获 `QuotaExceededError` 后停止本次新增；
4. 执行紧急清理；
5. 最多重试一次完整事务；
6. 再失败则明确降级，不产生半条记录或虚假索引。

### 5.4 分类 LRU

删除顺序：

1. 过期内容；
2. 已主动打开的完整附件；
3. 已主动打开的原图；
4. 长期未访问的缩略图；
5. 旧搜索快照；
6. 长期未访问的会话分段；
7. 无分段、快照或恢复状态引用的孤立消息。

同类先按 `lastAccessedAt` 从旧到新，时间接近时先删较大对象。

普通清理到 `effectiveTarget × 90%` 以下；紧急清理到
`effectiveTarget × 75%` 以下。

当前阅读窗口、正在打开的媒体和当前恢复状态的必要引用由 UI 提供临时保护集。保护只在
当前页面生命周期内有效。

## 6. 一致性、自愈和单写入者

E1 必须提供：

- 所有正文、分段、快照、统计更新的事务边界；
- 写入完成前不暴露索引引用；
- Blob 只有完整成功后 `complete=true`；
- 启动快速检查和可分批执行的完整修复；
- 总字节和分类字节重算；
- 段缺消息时标记部分缓存；
- 快照失效引用移除；
- Blob 缺失时删除媒体元数据；
- 数据库整体消失时重建空库，不解释为主机数据删除；
- 清理游标和修复进度持久化；
- 页面后台时可暂停，下次继续。

多标签页协调使用带到期时间的 IDB 写入租约，可辅以 BroadcastChannel 通知。
`onversionchange` 必须关闭旧连接，`onblocked` 必须给出可解释状态。租约过期后才允许
新页面接管，不能形成永久锁。

## 7. 更新与迁移

三个版本独立：

- `appShellVersion`
- `contentSchemaVersion`
- `resumeStateVersion`

兼容更新：

- 更新应用壳；
- 保留私人内容；
- 保留未超过 24 小时且引用有效的恢复状态；
- 不调用 `indexedDB.deleteDatabase()`。

不兼容更新：

- 破坏性 schema、缓存范围扩大、新敏感类别、权限变化或大量 Blob 迁移必须先确认；
- 小型元数据变更使用原子 `versionchange`；
- 大型变换使用影子数据库、分批复制、校验、活动指针切换和延迟删除；
- 失败或中断时继续使用旧库；
- Service Worker 不参与私人数据迁移。

## 8. 分阶段实施文件和门禁

### E1：私人缓存内核

预计改动：

- 新增 `web/offline-cache.js`
- 更新静态壳加载和预缓存白名单
- 新增 E1 单元/浏览器契约测试

门禁：

- schema、事务、字节统计、TTL、配额、分类 LRU、QuotaExceededError、自愈、清空；
- API 仍不进入 CacheStorage；
- 不接入生产正文 UI；
- 普通壳更新不删除私人 DB。

### E2：文字缓存与离线阅读

预计改动：

- `web/app.js`
- `web/index.html`
- `web/style.css`
- `web/service-worker.js`
- `tools/local_web.py` 的 no-store 响应边界
- E2 API/浏览器测试

门禁：

- 网络优先；
- 只缓存用户实际展示的白名单正文；
- 离线副本显示缓存和校验时间、过期状态、缺失边界和重新加载；
- 过期内容不自动展示；
- 完整应用壳可离线启动；
- 移动请求保持 GET-only。

### E3：离线搜索与状态恢复

预计改动：

- 新增 `web/offline-search-worker.js`
- 新增 `web/mobile-resume.js`
- 移动搜索/恢复 UI 与测试

门禁：

- 固定文案“仅搜索已缓存内容”；
- 只搜索缓存正文，不调用 AI；
- 分批扫描和渲染，可取消；
- 显示覆盖量、最近缓存时间和不完整提示；
- 24 小时内恢复，超过 24 小时回首页；
- 引用缺失或迁移中安全回首页。

### E4：缩略图、原图和附件

在第 3.6 节媒体 API 前置条件满足前保持 BLOCKED。

门禁：

- 缩略图只在实际展示成功后缓存；
- 原图和附件只在用户主动打开且完整校验后缓存；
- 不缓存失败、中断或零散 Range；
- 大 Blob 有 iPhone 专项压力证据；
- 媒体优先清理，不影响正文和应用壳。

### E5：更新、迁移和内部验收

门禁：

- 驱逐、损坏、时钟异常、30 天、小配额和突降配额测试；
- 应用壳更新隔离和迁移中断恢复；
- 1 GiB/2 GiB 逻辑测试；
- 桌面 v8.1 回归；
- 移动 GET-only 网络记录；
- DB/WAL/SHM 哈希前后一致；
- 完整 iPhone 调试与验收清单已准备。

## 9. E 节点硬性 NO-GO 清单

以下任何一项发生，立即停止：

- 私人正文或附件进入 `yunos-app-shell-*`；
- Service Worker 对 `/api/*` 返回旧响应；
- HTTP 失败后把旧副本伪装成在线成功；
- 本地读取更新 `validatedAt`；
- 过期记录进入普通离线搜索或阅读；
- 2 GiB 被描述为设备保证；
- QuotaExceededError 没有紧急清理和一次重试上限；
- 清理没有类别优先级；
- 原图或附件被预取；
- 普通应用壳更新删除私人 DB；
- 迁移失败破坏旧库；
- 索引、消息和 Blob 不一致时无法修复；
- 移动端产生 POST、PUT、PATCH、DELETE 或后台写回；
- 复制生产 SQLite/WAL/SHM；
- 缓存 AI、Runtime、认证信息、Tailnet 私人标识或本地路径；
- 超过 24 小时仍恢复旧页面；
- 桌面 v8.1 回归；
- 生产数据库内容或哈希发生变化。

## 10. E0 验收记录

已确认：

- HEAD 与产品指定权威提交一致；
- A-D 的应用壳、移动搜索和会话阅读契约可定位；
- 生产库通过只读 URI 核验稳定 ID；
- 当前 real-readonly 服务在 `127.0.0.1:8765` 运行，状态为
  `service_mode=real-readonly`、`readonly=true`、`db_mode=real`；
- 38 项 v8.2 A-D 聚焦测试通过；
- 3 个前端 JavaScript 文件通过语法检查；
- DB/WAL/SHM 审计前后哈希一致：
  - DB：`0000000000000000000000000000000000000000000000000000000000000000`
  - WAL：`0000000000000000000000000000000000000000000000000000000000000000`
  - SHM：`0000000000000000000000000000000000000000000000000000000000000000`
- E0 只新增产品设计基线与本文档。

待 E1-E5 完成：

- 私人缓存功能实现；
- 媒体 API 契约；
- 离线完整应用壳启动；
- no-store HTTP 边界；
- 自动化驱逐/配额/时钟/迁移测试；
- iPhone Safari/PWA 真机总验收。
