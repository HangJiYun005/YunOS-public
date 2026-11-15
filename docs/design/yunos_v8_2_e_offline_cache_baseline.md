YunOS v8.2-E 离线缓存设计基线 v0

节点名称： Offline Cache状态： E-0 设计基线已建立，等待仓库级实现审计权威基线： 89ecb51689393b77e25feb5972dade075ba96634前置节点： v8.2-D 已正式关闭后续节点： v8.2-F iPhone 真机总验收日期： 2026-07-25

1. 节点目标

v8.2-E 为 YunOS 移动端增加可控、可解释、可清理的离线阅读能力。

该节点只在 iPhone 浏览器/PWA 沙盒内保存用户已经访问过的阅读副本。Windows 主机继续持有唯一生产数据库与运行时，Tailscale 私有 HTTPS 架构保持不变。

目标能力：

缓存用户实际搜索、展开和阅读过的文字内容；

缓存已经展示过的缩略图；

原图和附件仅在用户主动打开后缓存；

设备离线、主机离线或 Tailnet 暂时不可达时，允许读取仍有效的本地副本；

离线搜索仅覆盖本机已经缓存的内容；

离线副本明确显示缓存时间和数据状态；

内容默认有效期为 30 天；

以约 1 GiB 为目标容量；

以 2 GiB 为 YunOS 自身逻辑上限；

根据浏览器实际配额动态降级；

自动执行过期清理与分级 LRU 清理；

普通 PWA 更新不得误删兼容的私人内容缓存；

缓存结构或权限范围变化时进行明确确认；

24 小时内恢复上一次移动状态，超过 24 小时回到首页。

2. 四条不可突破的红线

2.1 应用壳与私人内容缓存彻底分离

现有 Service Worker CacheStorage 继续只保存静态应用壳，例如：

HTML；

CSS；

JavaScript；

Manifest；

图标；

离线壳。

聊天正文、会话片段、搜索副本、图片、附件、缓存索引和阅读状态不得写入普通应用壳 CacheStorage。

应用壳缓存可以随静态资源版本自动更新。私人内容缓存采用独立名称、独立结构版本、独立清理规则和独立迁移流程。

2.2 离线内容必须明确标识

离线搜索界面必须显示：

仅搜索已缓存内容

每个离线结果或会话页至少能够显示：

缓存时间；

最近在线校验时间；

是否已经过期；

内容可能落后于主机；

当前缺失的消息、原图或附件；

恢复网络后的重新加载入口。

在线请求失败后不得直接展示旧缓存并伪装成最新在线结果。

2.3 1 GiB / 2 GiB 只作为产品策略

1 GiB：期望达到的目标容量；

2 GiB：YunOS 自身逻辑上限；

浏览器实际配额：运行时通过 StorageManager 估算；

最终可用容量：由 iOS、Safari/PWA、设备剩余空间和系统回收策略共同决定。

产品文案不得承诺设备一定可以保存 1 GiB 或 2 GiB。

所有写入必须处理：

QuotaExceededError；

配额估算不可用；

配额突然下降；

浏览器主动驱逐；

iOS 系统清理；

写入过程中断；

索引与实际内容不一致。

2.4 保持阅读副本定位

移动端缓存不包含：

SQLite 数据库副本；

数据库 WAL/SHM；

服务端索引文件；

AI 上下文；

Runtime 数据；

模型输出缓存；

Tailscale 令牌；

服务端秘密；

移动端写入队列；

双向同步信息。

移动业务请求继续保持 GET-only，不实现编辑、标注、上传、删除、同步和 Background Sync 写回。

3. 推荐总体架构

3.1 应用壳层

存储位置：

CacheStorage
└── yunos-app-shell-v8.2-e-*

职责：

启动 PWA；

加载静态 UI；

提供最小离线页面；

更新时清理旧应用壳；

永远不保存私人正文和附件。

3.2 私人内容层

首选存储位置：

IndexedDB
└── yunos-private-content

选择 IndexedDB 的原因：

支持结构化数据；

支持 Blob；

支持索引；

支持事务；

能记录每条内容的大小、时间、来源和优先级；

适合实现 TTL、LRU、统计和迁移；

可以让应用层明确决定何时读取离线副本；

避免 Service Worker 在缺少 UI 语境时静默返回旧 API 响应。

私人内容读取由前端数据访问层控制。Service Worker 对 /api/ 继续保持直接网络访问，不在 fetch 事件中把旧响应伪装成实时 API。

3.3 状态恢复层

状态恢复使用独立数据库：

IndexedDB
└── yunos-mobile-resume

它与正文缓存分离，只保存：

搜索词；

排序；

已加载结果引用；

列表滚动位置；

展开的多匹配；

展开的消息预览；

当前会话；

阅读锚点；

阅读滚动位置；

顶栏显示状态；

保存时间；

状态结构版本。

有效期固定为 24 小时。恢复失败、结构不兼容或引用内容已被清理时，安全回到移动首页。

4. 私人内容数据模型草案

4.1 数据库元信息 meta

关键字段：

key
value
schemaVersion
createdAt
updatedAt

应记录：

当前结构版本；

逻辑内容总字节数；

各内容类别字节数；

上次完整清理时间；

上次配额估算；

是否获得 persistent storage；

当前迁移状态；

当前清理锁；

上次索引修复结果。

4.2 消息正文 messages

主键建议：

[conversationId, messageId]

字段：

conversationId
messageId
role
text
messageTime
sourcePath
cachedAt
validatedAt
expiresAt
lastAccessedAt
byteSize
contentVersion

约束：

只保存 API 返回的阅读副本；

sourcePath 使用同源相对路径或逻辑来源，不保存私人 Tailnet 域名；

同一消息由稳定 ID 去重；

离线读取更新 lastAccessedAt，不延长 validatedAt；

在线重新获取并确认内容后更新 validatedAt 与 expiresAt。

4.3 会话分段 segments

主键建议：

segmentKey

字段：

conversationId
segmentKey
firstMessageId
lastMessageId
messageIds[]
anchorMessageId
direction
cachedAt
validatedAt
expiresAt
lastAccessedAt
byteSize

分段层只保存消息引用和边界，正文由 messages 去重存储。

这可以避免锚点窗口、向上加载和向下加载之间重复保存相同消息。

4.4 搜索快照 searchSnapshots

主键建议：

queryHash + sortMode

字段：

query
normalizedQuery
sortMode
resultRefs[]
cachedAt
validatedAt
expiresAt
lastAccessedAt
byteSize

快照用于恢复用户曾经执行过的在线搜索。离线时仍需要标注：

此结果来自某次已缓存搜索，不代表主机当前完整结果。

4.5 离线本地搜索

v8.2-E 初版范围：

仅搜索已缓存消息正文；

只做关键词/子串匹配；

不做语义搜索；

不调用移动端 AI；

不声称覆盖全部 YunOS 数据；

结果按本地匹配度和缓存时间排序；

最多逐批展示，避免一次扫描或渲染全部结果。

初版建议按“会话分段”建立规范化搜索文本，并在专用 Web Worker 中异步扫描 IndexedDB。性能测试未通过前，不提前引入高膨胀的中文 n-gram 倒排索引。

4.6 缩略图 thumbnails

字段：

mediaKey
conversationId
messageId
mimeType
blob
width
height
etag
lastModified
cachedAt
validatedAt
expiresAt
lastAccessedAt
byteSize

写入时机：

缩略图成功显示后；

仅保存当前实际展示的缩略图；

不因缩略图展示而下载原图。

4.7 原图和附件 mediaObjects

字段：

mediaKey
attachmentId
conversationId
messageId
kind
filename
mimeType
blob
etag
lastModified
contentLength
cachedAt
validatedAt
expiresAt
lastAccessedAt
byteSize
complete

写入时机：

用户主动打开原图；

用户主动打开附件；

响应完整成功；

完整性检查通过。

初版不缓存：

未被主动打开的原图；

仅在结果列表中出现的附件；

失败或中断下载；

无法确认完整性的部分响应；

Range 请求产生的零散片段。

大 Blob 在 iPhone 上必须进行专项压力测试。若 IndexedDB 大 Blob 稳定性或性能不达标，允许在后续实现审计中改为“独立私人媒体 CacheStorage + IndexedDB 元数据索引”，但仍须与应用壳缓存命名空间、清理逻辑和更新逻辑完全分离。

5. 30 天有效期

每条私人内容同时记录：

cachedAt：首次保存时间；

validatedAt：最近一次通过主机在线响应确认的时间；

expiresAt：validatedAt + 30 天；

lastAccessedAt：最近一次本地读取时间。

规则：

本地读取只更新 lastAccessedAt。

本地读取不得把旧内容重新变成“新内容”。

在线成功重新获取后才允许更新 validatedAt。

到达 expiresAt 后，该记录退出普通离线搜索和普通离线阅读。

过期记录进入最高优先级清理队列。

清理尚未执行时，页面不能把它当作有效副本自动展示。

当前页面已经打开且内容随后过期时，显示明确过期状态，并要求联网刷新或返回。

设备时间异常时，采用保守失效策略，不延长有效期。

6. 容量策略

6.1 运行时估算

优先调用：

navigator.storage.estimate()
navigator.storage.persisted()
navigator.storage.persist()

estimate() 返回的 usage 和 quota 只作为近似值。

persist() 只在用户明确启用离线缓存或首次保存较大内容时请求一次。返回 false 时继续提供降级缓存，界面不得宣称数据不会被系统清理。

6.2 动态容量

定义：

产品目标：1 GiB
产品逻辑上限：2 GiB

当 quota 可用时：

effectiveTarget  = min(1 GiB, quota × 50%)
effectiveCeiling = min(2 GiB, quota × 70%)

目的：

为应用壳、状态数据和浏览器内部开销保留空间；

避免私人缓存主动占满整个 origin 配额；

在小配额设备上自动缩小目标。

当 estimate() 不可用时，采用保守降级：

目标：256 MiB
逻辑上限：512 MiB

任何实际写入错误都优先于估算值。出现 QuotaExceededError 时立即停止当前新增写入，执行紧急清理，再进行最多一次重试。

7. 自动清理算法

7.1 触发条件

PWA 启动后低优先级调度；

私人内容成功写入后；

当前逻辑大小超过 effectiveTarget；

写入前预测将超过 effectiveCeiling；

捕获 QuotaExceededError；

最近一次清理超过 24 小时；

检测到索引与实际记录不一致；

缓存结构迁移结束后。

7.2 清理顺序

第一层：所有超过 30 天的内容。

第二层：按类别优先级清理：

已打开过的完整附件；

已打开过的原图；

长期未访问的缩略图；

旧搜索快照；

长期未访问的会话分段；

孤立且没有任何分段引用的消息正文。

同一类别内按 lastAccessedAt 从旧到新清理。时间接近时优先删除体积更大的对象。

7.3 清理目标

普通清理：

清理到 effectiveTarget 的 90% 以下

紧急清理：

清理到 effectiveTarget 的 75% 以下

这样可以减少连续写入、连续清理和反复触发配额错误。

7.4 保护内容

普通清理不得删除：

当前正在阅读的会话窗口；

当前正在下载或打开的媒体；

当前恢复状态直接引用的必要消息；

应用壳；

数据库元信息；

迁移日志；

清理锁。

保护只在当前会话生命周期内有效，不形成永久“收藏”功能。

7.5 非阻塞执行

每批删除限制条目数或字节数；

每批之间主动让出事件循环；

页面进入后台时允许暂停；

中断后下次继续；

清理进度保存在元信息中；

清理失败不破坏尚可读取的记录。

8. 在线与离线数据策略

8.1 在线状态

在线访问始终优先请求主机。

成功后：

页面展示在线结果；

异步写入或更新私人内容缓存；

更新 validatedAt；

不显示常驻连接提示。

8.2 请求失败

不单独依赖 navigator.onLine 判断。

当真实请求失败、超时或返回不可用状态时：

检查是否存在有效本地副本；

存在时显示离线副本；

显示缓存时间与范围；

提供重新加载；

不存在时显示“此内容未缓存”；

禁止显示在线空结果状态。

8.3 离线搜索文案

固定语义：

仅搜索已缓存内容

同时显示：

当前缓存覆盖的会话或消息数量；

最近缓存更新时间；

搜索结果可能不完整；

语义搜索不可用；

恢复连接后可搜索全部主机内容。

8.4 离线阅读

离线会话页允许：

阅读已缓存消息；

在已缓存片段之间滚动；

查看已缓存缩略图；

打开已完整缓存的原图或附件。

需要明确占位：

更早消息未缓存；

后续消息未缓存；

原图未缓存；

附件未缓存；

内容已经过期；

缓存已被系统清理。

9. 更新与迁移安全

9.1 版本分离

三个版本独立管理：

appShellVersion
contentSchemaVersion
resumeStateVersion

普通静态资源更新只改变 appShellVersion。

Service Worker 激活时只能清理明确属于旧应用壳的缓存名称，不得删除 IndexedDB 私人内容数据库。

9.2 兼容更新

当内容结构兼容时：

自动更新应用壳；

保留私人内容；

保留 24 小时状态；

新代码继续读取旧兼容记录。

9.3 不兼容更新

以下情况需要用户确认：

内容数据库结构发生破坏性变化；

缓存范围扩大；

新增敏感内容类别；

权限需求变化；

迁移可能长时间占用设备存储；

迁移需要创建大量新 Blob。

9.4 迁移策略

小型元数据变更：

使用 IndexedDB 原子 versionchange transaction；

失败时事务回滚并保留旧版本。

大型内容变换：

创建影子数据库；

分批复制与校验；

完成后切换活动版本指针；

切换前保留旧数据库；

失败或中断时继续使用旧数据库；

验证通过后再延迟删除旧数据库。

所有页面和 Service Worker 遵守单写入者租约，避免新旧版本同时写入不兼容结构。

10. 浏览器或系统回收后的自愈

WebKit 可能按 origin 整体驱逐数据。YunOS 无法保证离线副本永久存在。

启动时检查：

IndexedDB 数据库是否存在；

元信息记录是否存在；

记录总数与统计是否一致；

Blob 是否完整；

分段引用的消息是否存在；

搜索快照引用是否有效；

24 小时状态引用是否仍可恢复。

异常处理：

数据库整体消失：视为系统已经清理，重建空缓存；

元信息消失：重新统计；

Blob 缺失：删除对应媒体元数据；

分段缺消息：标记为部分缓存；

搜索引用失效：移除失效结果；

恢复状态失效：回到首页；

不把缓存损坏解释成主机数据被删除。

11. 安全与隐私

私人缓存只存在于当前 iPhone 的 Safari/PWA origin 沙盒中。

禁止写入：

Tailnet 完整域名；

Tailscale IP；

Tailscale 身份信息；

Cookie 备份；

Authorization 头；

服务端秘密；

本地数据库路径；

AI/Runtime 数据；

生产 SQLite 文件。

当前产品决策继续保持：

不增加 Face ID；

不增加 PIN；

不增加移动端二次登录。

因此必须在产品文档中明确本地风险：

能解锁 iPhone 的人可能访问已缓存内容；

删除 PWA 图标不一定等同于立即清理所有站点数据；

Safari“清除网站数据”会删除缓存；

iOS 存储压力可能自动删除缓存；

YunOS 提供“清空离线内容”入口时，必须删除正文、媒体、索引、统计和恢复状态。

12. 24 小时状态恢复

状态保存触发：

搜索词变化并稳定；

排序变化；

结果加载完成；

预览展开或收起；

进入会话；

阅读位置稳定；

页面进入后台；

pagehide；

PWA 即将关闭。

恢复判定：

当前时间 - savedAt <= 24 小时

同时要求：

状态结构版本兼容；

引用的会话和消息仍存在；

当前路由合法；

缓存迁移没有进行中；

不存在损坏标记。

任一条件失败时回到移动首页。

13. 实施拆分建议

v8.2-E0：仓库审计与设计关闭

审计现有 Service Worker、PWA、API、附件和状态恢复；

核对稳定 ID；

核对附件响应头与 Range 行为；

确认推荐架构；

完成基线文档；

不实现生产功能。

v8.2-E1：私人缓存内核

IndexedDB schema；

数据访问层；

事务写入；

大小统计；

TTL；

配额估算；

LRU；

QuotaExceededError；

自愈与清空。

v8.2-E2：文字缓存与离线阅读

消息去重；

会话分段；

在线写入；

网络失败回退；

离线标识；

缺失区间占位；

过期处理。

v8.2-E3：离线搜索与状态恢复

缓存搜索快照；

本地关键词搜索；

“仅搜索已缓存内容”；

24 小时状态恢复；

结构失效安全回首页。

v8.2-E4：缩略图、原图和附件

缩略图自动缓存；

原图/附件仅主动打开后缓存；

Blob 完整性；

大文件限制；

配额失败降级；

媒体清理优先级。

v8.2-E5：更新、迁移和完整内部验收

Service Worker 更新隔离；

schema 迁移；

中断恢复；

浏览器驱逐模拟；

30 天时间测试；

小配额测试；

1 GiB/2 GiB 逻辑测试；

桌面回归；

GET-only；

数据库门禁。

完成 E 后进入 v8.2-F，由 iPhone 执行完整真机总验收。

14. E 节点验收门禁

任何一项失败均为 NO-GO：

应用壳与私人内容没有彻底分离；

API 正文进入普通 CacheStorage；

离线结果缺少明确标识；

过期副本被当作实时结果；

读取缓存会延长在线校验时间；

2 GiB 被写成设备保证容量；

未处理 QuotaExceededError；

LRU 无类别优先级；

原图或附件在未打开时被预缓存；

更新 Service Worker 会清空私人内容；

迁移失败会破坏旧缓存；

缓存索引与 Blob 不一致时无法自愈；

产生移动端 POST/PUT/PATCH/DELETE；

复制生产 SQLite；

缓存 AI/Runtime；

写入 Tailnet 私人标识或认证秘密；

24 小时后仍恢复旧页面；

桌面 v8.1 出现回归；

生产数据库内容发生变化。

15. 当前结论

v8.2-E 已从产品层正式启动。

当前已锁定：

四条红线；

应用壳与私人内容分层；

IndexedDB 为私人内容首选存储；

网络优先、应用层显式离线回退；

30 天以最近在线校验时间计算；

本地读取不延长内容有效期；

1 GiB 目标、2 GiB 自身逻辑上限；

运行时配额自适应；

分类 LRU；

原图和附件仅主动打开后缓存；

Service Worker 更新与内容 schema 独立；

24 小时状态恢复；

不复制数据库、不写入、不同步、不缓存 AI/Runtime。

进入生产实现前仍需完成仓库级审计，重点确认：

当前 API 返回字段与稳定 ID；

当前附件、缩略图和原图路径；

Range、ETag、Last-Modified 与 Content-Length；

当前状态恢复数据结构；

当前 Service Worker 激活和旧缓存删除规则；

当前前端请求封装层；

当前测试入口和 iPhone 调试路径。
