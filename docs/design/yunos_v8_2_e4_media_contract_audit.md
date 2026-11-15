# YunOS v8.2-E4-0 Media Read-only Contract Audit

## 结论摘要

审计基线：

- 分支：`codex/v8.2-e4-media-contract-audit`
- HEAD：`34f06e211cebc77ce06a1fe4503b87707aaaca8c`
- E3 主功能提交：`a0dcf68bec6cbe2793d09bc11cb1e1b0be5675c0`
- E3 状态条补充修复：`9aa92508755fcf1b9f75e00c6aa224472a60570d`
- E3 最终测试同步提交：`34f06e211cebc77ce06a1fe4503b87707aaaca8c`

最终判定：**BLOCKED（禁止直接实现媒体缓存）**。

当前不存在合格的媒体 GET 契约。生产库确实保存媒体元数据，但当前存在四个硬阻断：

1. `messages.metadata_json.attachments` 中有 1,112 条附件元数据，当前三个消息接口均不返回
   附件对象或媒体 ID；
2. `assets` 表只有 172 条目录元数据，缺少规范的消息关系、MIME、完整性摘要和服务端可用
   路径；
3. 当前文件系统和导入包中找不到这 172 条目录记录对应的实体字节；
4. 服务器没有媒体路由、媒体 HEAD、Range、ETag、`nosniff`、安全
   `Content-Disposition` 或路径根目录验证。

因此 E4 不得直接进入 `thumbnails`、`mediaObjects`、原图或附件缓存实现。下一步必须先完成
E4-1：建立只读媒体索引和最小同源 GET/HEAD 契约。E4-0 本身不进入 E4-1。

## 1. 审计范围

本次只执行：

- 仓库级媒体入口和数据流审计；
- 生产 SQLite `mode=ro&immutable=1` 查询；
- 文件系统和导入包只读清点；
- `127.0.0.1:8784` real-readonly HTTP/Chrome 探针；
- 桌面 Chrome 的 Blob、IndexedDB、Object URL、CacheStorage 和请求方法验证；
- iOS/PWA 官方能力资料审计；
- 最小媒体 API、安全策略和分阶段实施设计。

本次未执行：

- 媒体缓存写入；
- 生产 SQLite、WAL、SHM 或媒体文件修改；
- `thumbnails`、`mediaObjects` 写入；
- IndexedDB schema 修改；
- 上传、编辑、删除、同步或后台同步；
- Tailscale、Serve、Funnel 或公网操作；
- E4-1 实现；
- E4 文档或探针的暂存、提交、push、merge 或 tag。

## 2. 当前媒体数据流

### 2.1 ChatGPT 初始导入

```text
ChatGPT 导出 ZIP
  ├─ conversations*.json
  │    └─ mapping[].message
  │         ├─ content.content_type
  │         ├─ content.parts
  │         └─ message.metadata
  └─ conversation_asset_file_names.json + *.dat 实体
         ↓
tools/chatgpt_export_importer.py
  ├─ extract_text_from_content()
  │    └─ 字符串保留；非文本对象转成文本或占位文本
  ├─ flatten_conversation()
  │    ├─ content_type → messages.content_type
  │    ├─ 抽取文本 → messages.text
  │    └─ message.metadata → messages.metadata_json
  └─ load_assets()
       └─ asset_file/original_name/size_bytes → assets
         ↓
生产 SQLite
  ├─ messages.metadata_json：附件、外部图像和 URL 元数据
  └─ assets：172 条资产目录元数据
```

代码锚点：

- `tools/chatgpt_export_importer.py:57`：非文本内容被压缩为文本/占位；
- `tools/chatgpt_export_importer.py:90`：消息仅保存 `content_type`、`text` 和原始
  `metadata_json`；
- `tools/chatgpt_export_importer.py:217`：资产目录只读取映射文件和 ZIP 条目大小；
- `tools/chatgpt_export_importer.py:342`：`assets` 只有三个字段。

### 2.2 增量导入

`tools/chatgpt_incremental_import.py:35` 的 `MESSAGE_COLUMNS` 继续保存
`content_type/text/metadata_json`，但增量执行只追加 conversations/messages
（`tools/chatgpt_incremental_import.py:416`），没有同步 `assets` 表。

这意味着当前 172 条 `assets` 记录只覆盖一部分历史附件；生产库中另有 940 个唯一附件 ID
没有对应 `assets` 目录行。

### 2.3 后端到浏览器

```text
messages.metadata_json / assets
         ↓ 当前后端未查询这些字段
GET /api/conversation
GET /api/conversation-messages
GET /api/message-context
         ↓
文本消息对象
  conversation_id/message_id/role/text/时间/来源
  （reader 额外返回 content_type）
         ↓
web/app.js
  messageBlock()/mobileReaderMessageElement()
         ↓
只渲染 message.text；没有图片、附件、打开、保存或下载入口
```

代码锚点：

- `tools/local_web.py:2048`：桌面/上下文消息 payload 只包含文本字段；
- `memory/conversation_reader.py:243`：移动 reader 返回 `content_type`，但不返回
  `metadata_json` 或附件；
- `web/app.js:3211`、`web/app.js:5484`：移动和桌面气泡都只渲染 `message.text`。

## 3. 生产数据统计

全部统计来自 `data/ayunos_chatgpt.db` 的 immutable 只读连接，报告不包含文件名、URL、
正文、完整路径或私人域名。

### 3.1 总量

| 指标 | 数量 |
|---|---:|
| 会话 | 409 |
| 消息 | 16,324 |
| 含媒体字段的会话 | 192 |
| 含媒体字段的消息 | 822 |
| 含附件数组的会话 | 172 |
| 含附件数组的消息 | 647 |
| 附件记录 | 1,112 |
| `assets` 目录记录 | 172 |
| `multimodal_text` 消息 | 610 |
| 图像引用数组 | 53 |
| 图像引用项 | 261 |
| 缩略图 URL 出现次数 | 279 |
| 原始内容 URL 出现次数 | 208 |
| 唯一媒体 URL 字符串 | 1,013 |

媒体 URL 出现次数中有 1,067 个 HTTPS 值和 2 个 `file:` 值。未对这些 URL 发出外网请求。

### 3.2 附件完整性

| 指标 | 结果 |
|---|---:|
| 非空附件 `id` | 1,112 / 1,112 |
| 唯一附件 `id` | 1,112 |
| 重复附件 `id` | 0 |
| 非空 `library_file_id` | 1,061 |
| 空/缺失 `library_file_id` | 51 |
| 重复 `library_file_id` | 0 |
| 空文件名 | 0 |
| 空 MIME | 33 |
| 大小未知 | 0 |
| 零字节记录 | 0 |
| MIME/扩展名需复核 | 131 |

131 条差异中包括 102 条扩展名为 JPEG、元数据 MIME 为 PNG 的记录，以及 ZIP、BAT、
CSV、RAR、M4A 的平台别名或危险格式差异。扩展名和导入 MIME 均不能单独作为响应 MIME
权威。

### 3.3 大小分布

| 大小 | 附件元数据 | `assets` 目录 |
|---|---:|---:|
| `<1 MiB` | 1,047 | 153 |
| `1–10 MiB` | 58 | 19 |
| `10–50 MiB` | 5 | 0 |
| `50–100 MiB` | 1 | 0 |
| `>100 MiB` | 1 | 0 |

附件元数据中的最大值为 129,584,985 字节。`assets` 最大值为 3,629,052 字节。

### 3.4 路径和隐私信号

消息元数据中检测到 56 个可能包含绝对路径或本机路径信号的值，分布于 46 条消息。它们
主要位于搜索引用片段、引用 URL 和外部图像 URL 元数据中，不得由媒体 API 原样返回或
写入日志。

## 4. 媒体类型矩阵

| 类别 | 数量 | 当前实体 | 当前展示 | E4 风险 |
|---|---:|---|---|---|
| 图片 | 707 | 无本地实体 | 仅文本/占位 | MIME、EXIF、动画、HEIC、原图预取 |
| 文本/代码文件 | 286 | 无本地实体 | 无下载入口 | 主动内容、编码、文件名注入 |
| HTML 等主动内容 | 18 | 无本地实体 | 无入口 | XSS，必须 attachment |
| 其他未知二进制 | 19 | 无本地实体 | 无入口 | 禁止内联，需嗅探/降级 |
| Office | 40 | 无本地实体 | 无入口 | iOS 预览差异、大文件 |
| PDF | 19 | 无本地实体 | 无入口 | Range、内联、超大文件 |
| 归档 | 21 | 无本地实体 | 无入口 | ZIP/RAR 只下载 |
| 视频 | 1 | 无本地实体 | 无入口 | Range、后台中断 |
| 音频 | 1 | 无本地实体 | 无入口 | Range、后台中断 |

另有 25 条记录使用 HTML/SVG 等主动内容扩展名，应强制下载或进入隔离预览，不得作为
可信同源文档直接内联。

## 5. 当前稳定 ID

### 5.1 可用候选

`messages.metadata_json.attachments[].id` 在当前快照中：

- 1,112 条全部非空；
- 1,112 条全部唯一；
- 172 条可通过现有导入命名规则关联到 `assets.asset_file`。

这是当前最好的导入源稳定 ID 候选。`library_file_id` 也具有唯一性，但缺失 51 条，因此
不能作为全量主键。

### 5.2 尚不合格的原因

附件 `id` 目前仍不构成合格的外部 `mediaId`：

- 只埋在 JSON 元数据中；
- API 不返回；
- 没有数据库级 `conversation_id + message_id + attachment_id` 关系约束；
- `assets` 表没有 conversation/message 外键；
- 没有实体路径根、内容哈希、MIME 复核或完整性状态；
- 940 个附件 ID 不在 `assets` 表；
- 现有资产字节全部缺失。

261 个外部图像引用的 `image_ref_id` 全部为空；原图 URL 和缩略图 URL 不能当作长期稳定
ID。

### 5.3 最小 ID 方案

建议在服务端只读媒体索引中生成：

```text
mediaId = "m_" + SHA-256(
  source_platform + "\0" + attachment_source_id
)
```

并保存/重建以下绑定：

```text
mediaId
sourcePlatform
sourceAttachmentId
conversationId
messageId
kind
variant
entityRootId
relativeEntityPath
verifiedMime
contentLength
contentSha256
```

哈希输入使用导入源稳定附件 ID，不使用客户端随机 UUID、数组序号、绝对路径、URL 或
Tailnet 标识。仅有 mediaId 仍不代表授权；服务器必须再次验证消息和会话关系。

## 6. 当前 GET 接口

当前返回消息的 GET 接口：

| 路径 | 方法 | 媒体字段 | 响应 |
|---|---|---|---|
| `/api/conversation` | GET | 无 | JSON 文本消息 |
| `/api/conversation-messages` | GET | 仅有 `content_type` | JSON 文本消息 |
| `/api/message-context` | GET | 无 | JSON 文本消息 |

`tools/local_web.py:545` 的 API 分派中没有媒体、附件、缩略图、原图或下载路由。

**当前不存在合格的媒体 GET 契约。**

静态 `web/` 文件处理器不算媒体契约；私人资产不在 Web 根目录，也不得通过拼接路径暴露。

## 7. 当前响应头

真实 8784 real-readonly 探针：

| 响应 | 状态 | Content-Type | Length | no-store | ETag | Last-Modified | Range | nosniff |
|---|---:|---|---|---|---|---|---|---|
| conversation JSON | 200 | JSON UTF-8 | 有 | 有 | 无 | 无 | 无 | 无 |
| reader JSON | 200 | JSON UTF-8 | 有 | 有 | 无 | 无 | 无 | 无 |
| context JSON | 200 | JSON UTF-8 | 有 | 有 | 无 | 无 | 无 | 无 |
| 不存在的媒体 GET | 404 | JSON UTF-8 | 有 | 有 | 无 | 无 | 无 | 无 |
| 不存在的媒体 HEAD | 404 | HTML | 有 | 无 | 无 | 无 | 无 | 无 |

`tools/local_web.py:476` 当前统一 JSON 响应已经设置 `Cache-Control: no-store`，这是相对
E0 旧审计的已完成修复。但 `tools/local_web.py` 没有 `do_HEAD()`；API HEAD 会落入静态
处理器，造成 GET/HEAD 错误格式和缓存头不一致。

## 8. 缩略图现状

- 数据库元数据有 279 个缩略图 URL 出现；
- 外部图像引用有 261 项；
- 没有非空 `image_ref_id`；
- 没有本地缩略图实体；
- 没有缩略图 GET 路由；
- 当前前端不展示这些缩略图；
- `web/offline-cache.js:70` 已预留 `thumbnails` store，但生产 UI 没有
  `putRecord("thumbnails", ...)`；
- Chrome 验收后 `thumbnails=0`。

外部 thumbnail URL 只是导入元数据提示，不是稳定、同源、安全的缓存来源。浏览器 CSS
缩小原图也不构成缩略图契约。

E4 初版应保持“不支持缩略图缓存”，直到 E4-1 能提供稳定 ID 和实体。以后如生成缩略图：

- 用户实际需要展示时按需生成，不预取原图；
- 最大边建议 512 CSS-independent pixels；
- 保持宽高比并处理 EXIF 方向；
- 动画 GIF/WebP 首版可静态首帧并明确标识；
- HEIC 只在解码器明确可用时生成；
- 输出固定为安全 JPEG/WebP，移除主动内容；
- 生成失败回退为类型占位，不回退到静默预取原图；
- 派生文件使用独立根目录和独立身份；
- 本次不实现生成器。

## 9. 原图现状

- 707 条附件按 MIME/扩展名归类为图片；
- 外部元数据有 208 个原始内容 URL；
- `assets` 中有 146 条图片目录记录；
- 当前本地实体为 0；
- 当前没有原图路由、打开动作或原图缓存触发。

原图 URL 可能失效、重定向、签名或跨域，不能由客户端直接当作持久身份。E4-3 必须只在
用户主动打开原图后缓存已完整校验的响应。

## 10. 附件现状

- 1,112 条附件元数据；
- 全部有导入源附件 `id`、名称和大小；
- 33 条 MIME 缺失；
- 51 条 `library_file_id` 缺失；
- 172 条有历史 `assets` 目录记录；
- 940 条没有资产目录记录；
- 所有目录记录均找不到实体字节；
- 当前无打开、下载、分享或保存入口。

导入器目前把部分非文本内容转成占位文本，因此用户只能看到文本提示，不可能通过前端可靠
恢复附件。

## 11. 外部 URL 现状

媒体相关元数据包含 1,013 个唯一 URL 字符串。审计未访问这些 URL，原因是：

- 可能含私人查询、签名或短期令牌；
- 可能已失效或重定向到登录页；
- 当前没有允许域名、DNS/IP、重定向或内容长度策略；
- 直接服务端代理会引入 SSRF；
- URL 不是稳定媒体身份。

E4-1 不应提供任意 URL 代理。外部 URL 媒体只有在导入关系、允许来源、重定向策略、私网
地址阻断、MIME 和大小门禁全部明确后才能纳入。

## 12. 缺失文件和孤立记录

| 状态 | 数量 | 解释 |
|---|---:|---|
| 附件元数据无 `assets` 行 | 940 | 增量导入未同步资产目录或源包不含映射 |
| `assets` 行无当前实体文件 | 172 | 当前导入包只有数据库/目录/报告，没有 `*.dat` 实体 |
| 当前文件系统 `.dat` 实体 | 0 | 无可供媒体路由读取的用户实体 |
| 文件存在但 DB 无引用 | 0 | 没有用户媒体实体可形成孤立文件 |
| 可计算重复实体哈希 | 不可计算 | 没有实体字节 |

“0 个孤立文件”不是完整性通过，而是因为实体集合为空。

## 13. 路径和权限边界

当前不存在媒体路由，因此以下探测全部返回统一 404 JSON：

- 编码 `../`；
- `..\`；
- 双重编码；
- Windows 盘符；
- UNC；
- 编码反斜杠。

这只能证明“当前没有路由”，不能证明未来媒体路径解析安全。

E4-1 必须：

1. 路由只接受 opaque `mediaId` 和枚举 `variant`，不接受路径；
2. 通过数据库索引解析到预先登记的 `entityRootId + relativePath`；
3. 对根目录和目标执行规范化/真实路径解析；
4. 拒绝 `..`、绝对路径、UNC、盘符、ADS、设备名、双重编码；
5. 拒绝 reparse point/symlink 逃逸；
6. 最终路径必须严格位于允许根内；
7. 验证 media → message → conversation 关系；
8. 未知 ID 和无关系 ID 都返回不泄露差异的 404；
9. 已知且有权访问但实体已丢失可返回脱敏 410。

`tools/local_web.py:613` 当前通用异常分支会把 `FileNotFoundError/ValueError` 的字符串写入
500 JSON。媒体路由不得复用这个行为，否则可能暴露服务器绝对路径。

## 14. MIME 与危险格式策略

当前数据含 HTML、SVG、BAT、可执行下载类型、Python、归档、Office 和未知二进制，并有
131 条 MIME/扩展名差异。

E4-1 MIME 决策顺序：

1. 受控索引中的导入 MIME；
2. 服务端有限字节签名检测；
3. 扩展名仅作提示；
4. 三者严重冲突时拒绝内联并强制下载；
5. 未知类型使用 `application/octet-stream`。

所有响应设置：

```http
X-Content-Type-Options: nosniff
Cross-Origin-Resource-Policy: same-origin
Referrer-Policy: no-referrer
```

HTML、SVG、JavaScript、BAT、可执行文件和未知主动内容必须：

```http
Content-Disposition: attachment
Content-Security-Policy: sandbox; default-src 'none'
```

不得把错误页、登录页、代理 HTML 或 MIME 严重冲突的内容写入私人媒体层。

## 15. Content-Disposition 方案

安全图片、音频、视频和 PDF 可以在明确产品允许时使用 `inline`；Office、归档、代码、
HTML/SVG/JavaScript、可执行和未知二进制使用 `attachment`。

文件名处理：

- 去除 CR/LF、NUL、引号和路径分隔符；
- 去除 `.`/`..` 和 Windows 保留设备名；
- 限制 UTF-8 字节长度；
- 提供安全 ASCII fallback；
- 使用 RFC 5987/6266 风格的 `filename*`；
- 不把服务器路径或原始私人目录写入响应头；
- 发生清洗失败时使用通用名称。

当前接口没有 `Content-Disposition`。

## 16. ETag 与 Last-Modified

当前 JSON 没有 ETag/Last-Modified。静态图标只有 Last-Modified，没有 ETag；这不是正式
媒体契约。

建议：

- 已有内容 SHA-256：使用强 ETag `"sha256-<digest>"`；
- 暂无哈希：在只读索引阶段流式计算并记录，不能每次把全文件读入内存；
- Last-Modified 来自受控实体元数据，不来自客户端名称或 URL；
- ETag 优先于 Last-Modified；
- 支持 `If-None-Match`、`If-Modified-Since` 和 304；
- `Cache-Control: no-store` 继续保留，条件请求只供 YunOS 主动重新验证；
- ETag 变化时先完整取得并校验新 Blob，再原子替换旧副本。

## 17. Range 与大文件策略

### 17.1 当前实测

对安全的 Web 根静态 PNG 样本进行服务器能力探测：

- 完整 GET：200、完整 2,084 字节；
- HEAD：200、无正文；
- `bytes=0-99`：返回 200 和完整 2,084 字节；
- `bytes=100-`：返回 200 和完整文件；
- `bytes=-100`：返回 200 和完整文件；
- 越界 Range：返回 200 和完整文件；
- 多 Range：返回 200 和完整文件；
- 4 路并发完整 GET：全部 200；
- 无 `Accept-Ranges`、`Content-Range` 或 ETag。

结论：当前 Python 静态处理器忽略 Range。该结果仅说明服务器基础能力，不能当作正式媒体
接口。

不存在媒体实体和媒体路由，因此无法真实验证：

- 媒体 206/416；
- 空实体；
- 大文件流式读取；
- 中途取消后的服务端停止；
- 读取时文件移动/删除；
- Range 片段完整性。

为避免用 mock 冒充产品证据，本节点将这些项目列为 BLOCKED，不创建生产媒体副本。

### 17.2 E4-1 要求

真实数据包含一个超过 100 MiB 附件、一个 50–100 MiB 附件、音频、视频和 19 个 PDF，
因此 E4 初版需要单 Range 支持：

- 200 完整流式 GET；
- HEAD 与 GET 同头但无正文；
- 206 + `Accept-Ranges: bytes` + `Content-Range`；
- 416 + `Content-Range: bytes */<total>`；
- 首段、开放尾段、后缀段；
- 拒绝多 Range 或设置严格上限；
- 限制单次片段、并发数和小 Range 滥用；
- 客户端取消后停止文件读取；
- 不把 Range 片段写入 `mediaObjects`；
- 只有完整响应校验成功后设置 `complete=true`。

## 18. no-store 与私人缓存层

真实 Chrome 已验证：

- JSON 私人响应有 `Cache-Control: no-store`；
- 17,975 字节 JSON 响应可完整读取为 Blob，长度与 Content-Length 一致；
- `no-store` 不妨碍应用层读取响应体；
- Service Worker 对 `/api/*` 直接联网（`web/service-worker.js:79`）；
- CacheStorage 只有 `yunos-app-shell-v8.2-e3-2` 的应用壳；
- CacheStorage 中 `/api/*` 和 `/api/media/*` 均为 0；
- `thumbnails=0`、`mediaObjects=0`；
- 生产页面网络方法只有 GET；审计上下文额外发出安全 HEAD；
- 页面错误 0、非预期控制台错误 0；
- 主动探测的 404 被单独记录为预期控制台事件。

隔离浏览器能力探针还验证：

- 4,096 字节 Blob 可写入并读回临时 IndexedDB；
- 字节逐项一致；
- Object URL 可读并已 revoke；
- 隔离测试数据库已删除；
- 未写入 YunOS 私人媒体 store。

`web/offline-cache.js` 已有 30 天 TTL、1 GiB 产品目标、2 GiB 产品上限和媒体完整性修复
逻辑，但这只是存储内核能力，不代表媒体来源契约已存在。

## 19. 建议的最小 GET 契约

### 19.1 路由

```http
GET  /api/media/{mediaId}?variant=attachment|original|thumbnail
HEAD /api/media/{mediaId}?variant=attachment|original|thumbnail
```

不新增 POST、PUT、PATCH 或 DELETE。

### 19.2 成功

```http
200 OK
Content-Type: <verified MIME>
Content-Length: <bytes>
Content-Disposition: inline|attachment; filename="safe"; filename*=UTF-8''...
Cache-Control: no-store
X-Content-Type-Options: nosniff
Cross-Origin-Resource-Policy: same-origin
Referrer-Policy: no-referrer
ETag: "sha256-..."
Last-Modified: ...
Accept-Ranges: bytes
```

### 19.3 Range

```http
206 Partial Content
Accept-Ranges: bytes
Content-Range: bytes <start>-<end>/<total>
Content-Length: <segment length>
```

无效或拒绝的 Range：

```http
416 Range Not Satisfiable
Content-Range: bytes */<total>
Cache-Control: no-store
```

### 19.4 条件与失败

- 304：ETag/Last-Modified 未变化；
- 400：非法 variant 或格式错误的 mediaId；
- 404：未知、无权或关系不成立；
- 410：已知且有权的媒体实体已经丢失；
- 416：Range 不合法；
- 500：固定脱敏错误码，不返回路径、堆栈、文件名或正文。

## 20. 建议的数据模型映射

不修改现有生产 SQLite 的前提下，先创建可审计的只读派生索引或独立 sidecar：

```text
media_index
  media_id                 PRIMARY KEY
  source_platform
  source_attachment_id
  source_library_file_id   NULLABLE
  conversation_id
  message_id
  kind                     attachment|original|thumbnail
  variant
  entity_root_id
  relative_entity_path
  original_name
  verified_mime
  content_length
  content_sha256
  source_updated_at
  entity_state             ready|missing|external|blocked
```

约束：

- `(source_platform, source_attachment_id, kind, variant)` 唯一；
- 关系必须能回到 message/conversation；
- 不保存绝对路径、Tailnet 域名、Cookie、令牌或签名 URL；
- 外部 URL 只保存受控来源引用，不作为路由路径；
- 缺失实体保留 `missing` 状态，API 不尝试猜路径；
- 生成缩略图具有独立 mediaId，不复用原图身份。

## 21. iOS/PWA 限制

官方资料和本地 Chrome 证据必须分开解释。

已知平台能力：

- IndexedDB 使用 structured clone，Blob 属于可克隆类型
  （[MDN structured clone](https://developer.mozilla.org/en-US/docs/Web/API/Web_Workers_API/Structured_clone_algorithm)）；
- Object URL 可引用 Blob，使用后必须 revoke；Service Worker 中不可用，以避免长期内存泄漏
  （[MDN createObjectURL](https://developer.mozilla.org/en-US/docs/Web/API/URL/createObjectURL_static)）；
- WebKit 的 Safari 17+ 支持 Storage API，Home Screen Web App 与浏览器应用具有同类 origin/
  overall quota，但默认仍是 best-effort，可因总配额、系统压力或长期未交互被驱逐；持久模式按
  启发式授予
  （[WebKit storage policy](https://webkit.org/blog/14403/updates-to-storage-policy/)）；
- `navigator.storage.estimate()` 只给近似 usage/quota，不能当作可保证空间
  （[MDN StorageManager.estimate](https://developer.mozilla.org/en-US/docs/Web/API/StorageManager/estimate)）；
- Safari Lockdown Mode 可以禁用 IndexedDB、File API 和 FileReader，因此必须有“仅在线/
  不可缓存”降级
  （[WebKit Safari 17 features](https://webkit.org/blog/14445/webkit-features-in-safari-17-0/)）；
- Web Share 支持文件但必须以真实用户激活触发，并在每次分享前调用 `canShare()`
  （[MDN Web Share API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Share_API)、
  [MDN Navigator.share](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/share)）；
- Safari 17+ 支持 HEIC，但具体解码、内存和 PWA 行为仍需设备验证
  （[WebKit HEIC support](https://webkit.org/blog/14445/webkit-features-in-safari-17-0/)）；
- Safari on iOS 能预览 PDF、Office/iWork 和 RTF，但该 Apple 文档不能替代当前 PWA、
  下载和分享流程实测
  （[Apple Safari content types](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/SafariWebContent/CreatingContentforSafarioniPhone/CreatingContentforSafarioniPhone.html)）；
- 后台页面会受到计时器和资源策略限制，`visibilitychange` 只是状态信号，不能保证下载和
  写入在 PWA 被杀死前完成
  （[MDN Page Visibility](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API)）。

桌面 Chrome 结果不等于 iPhone WebKit。以下项目仍必须留给 v8.2-F 真机。

## 22. E4 与 v8.2-F 真机测试矩阵

| 能力 | E4 桌面 Chrome | v8.2-F iPhone Safari | v8.2-F 主屏幕 PWA |
|---|---|---|---|
| 小 Blob IDB 写入/读回 | 已通过 4 KiB | 必测 | 必测 |
| 1/10/50/100+ MiB Blob | 未测，无实体 | 必测 | 必测 |
| Object URL 打开/revoke | 已通过 | 必测 | 必测 |
| GET/HEAD/单 Range | 当前无契约 | 必测 | 必测 |
| 视频/音频 Range 播放 | 当前无实体 | 必测 | 必测 |
| PDF 内联/下载 | 当前无入口 | 必测 | 必测 |
| ZIP/Office 下载/预览 | 当前无入口 | 必测 | 必测 |
| HEIC 原图/缩略图 | 当前无实体 | 必测 | 必测 |
| Web Share 文件 | API 存在但未触发 | 必测 `canShare` | 必测 `canShare` |
| 后台化时中断 | 未测 | 必测 | 必测 |
| PWA 被杀死时未完成写入 | 不适用 | 不适用 | 必测 |
| 存储 estimate/persist | Chrome estimate 有、未持久 | 必测 | 必测 |
| 系统压力驱逐 | 未测 | 必测 | 必测 |
| 30 天 TTL/LRU | 内核已有 | 必测 | 必测 |
| 应用壳更新后媒体保留 | 未实现 | 必测 | 必测 |
| Lockdown Mode 降级 | 未测 | 必测 | 必测 |

验收要求：未完整校验的 Blob 永不标记 `complete=true`，中断后不保留可见的半成品。

## 23. 安全威胁清单

| 威胁 | 当前状态 | E4-1 门禁 |
|---|---|---|
| 路径穿越/UNC/盘符/ADS | 无路由，未暴露 | opaque ID + 根目录规范化 |
| symlink/reparse escape | 无路由，未验证 | real path 必须留在允许根 |
| IDOR | 无媒体 ID | 验证 media-message-conversation |
| SSRF | 当前未代理 URL | 禁止任意 URL，受控来源和私网阻断 |
| MIME 欺骗 | 131 条需复核 | 元数据 + 签名检测 + nosniff |
| HTML/SVG/JS 主动内容 | 数据存在 | 强制 attachment/sandbox |
| 响应头注入 | 当前无 disposition | 文件名清洗、禁止 CRLF |
| 大文件内存耗尽 | 最大约 123.6 MiB | 流式、Range、并发限制 |
| 小 Range 滥用 | 无 Range | 单 Range 和频率/长度限制 |
| 不完整 Blob | 当前无缓存 | 原子完成、长度/哈希校验 |
| 浏览器普通 HTTP 缓存 | JSON 已 no-store | 媒体继续 no-store |
| CacheStorage 私人副本 | 当前 0 | Service Worker 继续绕过 API |
| 本机路径泄露 | 元数据有 56 个信号 | payload/log 全部脱敏 |
| Token/Cookie/Tailnet 泄露 | 未发现媒体响应 | 不进入索引、响应、日志 |
| 错误堆栈/路径泄露 | 通用异常存在风险 | 媒体专用脱敏错误映射 |

## 24. BLOCKED 项

1. 当前没有媒体 GET/HEAD 路由；
2. 1,112 条附件元数据没有规范关系表；
3. 940 条附件没有资产目录行；
4. 172 条资产目录记录全部缺失实体字节；
5. 图像引用没有稳定 `image_ref_id`；
6. 当前 API 不返回任何附件或 mediaId；
7. 当前 HEAD 行为不一致：媒体 GET 404 JSON/no-store，HEAD 404 HTML/无 no-store；
8. 没有安全 Content-Disposition、nosniff、ETag 或 Range；
9. 没有根目录、symlink/reparse、IDOR 或 SSRF 实现可供验收；
10. 没有实体字节，无法验证哈希、重复文件、空文件、删除竞态或真实大文件 Range；
11. 大 Blob、后台中断、系统驱逐、分享和格式预览尚无 iPhone/PWA 证据。

## 25. 推荐实施拆分

### E4-1A：只读媒体索引与实体恢复门禁

- 从 `messages.metadata_json.attachments[].id` 建立规范关系；
- 解释 940 条无目录附件；
- 以独立、可审计方式恢复或定位实体，不修改生产 SQLite；
- 记录 missing/external/blocked 状态；
- 生成稳定 mediaId、内容长度、MIME 和哈希；
- 验证无绝对路径和私人基础设施信息。

### E4-1B：最小同源媒体 API

- GET/HEAD-only；
- IDOR、根目录、reparse/symlink 和 SSRF 安全；
- Content-Type/Length/Disposition；
- no-store/nosniff；
- ETag/Last-Modified/304；
- 单 Range/206/416；
- 流式、取消和并发限制；
- 全套安全契约测试。

### E4-2：缩略图

- 只缓存实际展示成功的缩略图；
- 无稳定缩略图时使用占位；
- 独立 ID、元数据、30 天 TTL 和 LRU；
- 不预取原图；
- 动画、EXIF、HEIC、颜色和失败回退门禁。

### E4-3：原图

- 用户主动打开后缓存；
- 完整性、配额、Object URL 和 revoke；
- ETag 变化时原子替换；
- 不缓存失败、HTML 错误页或 Range 片段。

### E4-4：附件

- 用户主动打开或下载后缓存；
- 大文件、单 Range、分享和安全下载；
- 危险 MIME 强制 attachment；
- 中断时安全失败；
- iOS PDF/ZIP/Office/音视频验收。

### E4-5：内部总验收

- 小配额、驱逐、30 天 TTL、LRU；
- 应用壳更新隔离；
- 数据库和文件门禁；
- GET/HEAD-only；
- 桌面回归；
- iPhone Web Inspector 真机矩阵。

## 26. 最终判定

**BLOCKED。**

当前真实媒体来源已经定位，但没有可供浏览器安全读取的实体契约。必须先完成 E4-1A/E4-1B
并通过安全和真机前置门禁，才可以进入缩略图、原图或附件缓存。

本节点停止时保持：

- 生产功能代码未修改；
- E4 媒体缓存未实现；
- `thumbnails=0`；
- `mediaObjects=0`；
- E4 文档和 `output/` 证据未暂存、未提交；
- 暂存区为空；
- 未进入 E4-1；
- 未 push、merge 或 tag。

## 附录 A：审计文件

核心代码：

- `tools/chatgpt_export_importer.py`
- `tools/chatgpt_incremental_import.py`
- `memory/conversation_reader.py`
- `tools/local_web.py`
- `web/app.js`
- `web/offline-cache.js`
- `web/service-worker.js`
- `web/pwa.js`

设计与测试基线：

- `docs/design/yunos_v8_2_e_offline_cache_e0_audit.md`
- `docs/design/yunos_v8_2_e_offline_cache_baseline.md`
- `tests/test_v82_private_cache_core.py`
- `tests/test_v82_text_cache_offline_reader.py`
- `tests/test_v82_offline_search_state_restore.py`
- `tests/browser/v82_private_cache_core.js`
- `tests/browser/v82_text_cache_offline_reader.js`
- `tests/browser/v82_offline_search_state_restore.js`
- `tests/browser/run_browser_callback.js`

只读证据：

- `output/playwright/v8.2-e4-media-contract-audit/readonly_inventory.json`
- `output/playwright/v8.2-e4-media-contract-audit/readonly_inventory_probe.py`
- `output/playwright/v8.2-e4-media-contract-audit/media_contract_browser_probe.js`
- `output/playwright/v8.2-e4-media-contract-audit/run_browser_probe.ps1`
