# YunOS v8.2-E4-1A Read-only Media Index and Entity Recovery Gate

## 结论摘要

E4-1A 已建立只读媒体索引、稳定 opaque `mediaId`、完整关系验证、实体状态机、
受控根路径门禁和未来 E4-1B 所需的内部 descriptor。实现不接入 HTTP，不返回媒体
字节，不访问外部 URL，不修改 SQLite，也不写入任何媒体缓存。

真实数据库结果：

- 附件记录：1,112；
- 稳定原始 ID：1,112；
- 唯一公开 `mediaId`：1,112；
- 派生冲突：0；
- 有效 attachment → message → conversation 关系：1,112；
- 无资产目录行：940；
- 有资产目录行但实体缺失：172；
- `entity_available`：0；
- 附件声明大小与资产目录声明大小不一致：13。

E4-1A 节点实现和内部验收通过，但 **E4-1B 仍为 BLOCKED**。阻断原因不是索引失败，
而是固定允许根中没有任何 `.dat` 实体，无法形成一条真实 `entity_available` 记录来
验收 GET/HEAD、Range、内容头和删除竞态。

## 1. 输入数据源

索引只读取以下生产数据：

- `data/ayunos_chatgpt.db`；
- `messages.message_id`；
- `messages.conversation_id`；
- `messages.source_platform`；
- `messages.metadata_json.attachments[]`；
- `conversations.conversation_id`；
- `assets.asset_file`；
- `assets.original_name`；
- `assets.size_bytes`。

连接使用 SQLite URI `mode=ro&immutable=1` 并设置 `PRAGMA query_only=ON`。查询不读取
消息正文和会话标题。索引不创建表、不迁移 schema、不写 sidecar。

当前资产目录的 172 个 locator 均为不含路径分隔符的 `.dat` 叶名，并且均精确满足：

```text
asset_file = source attachment id + ".dat"
```

没有使用模糊子串匹配、前端路径、文件名搜索或磁盘递归猜测来建立资产关系。

## 2. 1,112 条附件的分类统计

### 2.1 关系与实体状态

| 项目 | 数量 |
| --- | ---: |
| 附件记录 | 1,112 |
| 有效关系 | 1,112 |
| `invalid_relationship` | 0 |
| `asset_record_missing` | 940 |
| `entity_missing` | 172 |
| `entity_available` | 0 |
| 资产目录映射 | 172 |
| 附件/资产目录声明大小冲突 | 13 |

### 2.2 声明大小区间

| 区间 | 数量 |
| --- | ---: |
| 小于 1 MiB | 1,047 |
| 1–10 MiB | 58 |
| 10–50 MiB | 5 |
| 50–100 MiB | 1 |
| 大于等于 100 MiB | 1 |
| 未知 | 0 |

### 2.3 声明 MIME

| MIME | 数量 |
| --- | ---: |
| `image/png` | 425 |
| `image/jpeg` | 240 |
| `text/markdown` | 225 |
| `text/plain` | 34 |
| 空 MIME | 33 |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | 30 |
| `image/bmp` | 20 |
| `application/zip` | 19 |
| `application/pdf` | 18 |
| `text/html` | 17 |
| `text/x-python` | 12 |
| `image/svg+xml` | 7 |
| `application/json` | 6 |
| `image/webp` | 6 |
| `application/x-msdos-program` | 4 |
| `application/msword` | 3 |
| `application/x-python-code` | 3 |
| `application/vnd.rar` | 2 |
| `model/step` | 2 |
| `text/csv` | 2 |
| `application/x-msdownload` | 1 |
| `audio/x-m4a` | 1 |
| `image/avif` | 1 |
| `video/mp4` | 1 |

主动内容、可执行格式、空 MIME 和 MIME 不一致均不能仅凭扩展名获得内联打开资格。

## 3. 稳定 mediaId 方案

公开 ID 格式：

```text
ym1_<52 个小写 Base32 字符>
```

派生输入：

```text
SHA-256(
  "yunos-media-id-v1\0"
  || normalized source_platform
  || "\0"
  || source attachment id
)
```

SHA-256 的 32 字节结果使用无填充小写 Base32。该方案具备：

- 固定版本域隔离；
- 同一附件跨重启稳定；
- 不依赖数组顺序；
- 不依赖文件路径或 Tailnet；
- 不依赖前端随机值；
- 不暴露原 attachment id；
- 不因应用壳版本变化；
- 文件在允许根内移动不会改变逻辑身份；
- 1,112 个真实记录均得到唯一 ID。

如果同一个派生 ID 指向不同源身份，构建立即抛出安全的
`MediaIndexConflict("Media identity collision")`，不保留任一映射。重复原始附件身份
归类为 `invalid_relationship`，不能通过 mediaId 查询。

## 4. 关系验证模型

内部关系链为：

```text
mediaId
  → normalized source platform + source attachment id
  → attachment metadata in one message row
  → unique message id
  → existing conversation id
```

构建时验证：

- attachment 必须是对象；
- 原 attachment id 必须非空且格式可处理；
- message id 必须非空且全库唯一；
- conversation 必须存在；
- 原 attachment 身份不能重复；
- 派生 mediaId 不能碰撞；
- 资产目录只接受精确的 `<attachment-id>.dat`；
- attachment index 只保留为诊断辅助，不参与 ID 派生。

未来受权调用必须同时传入预期 message id 和 conversation id。只提供 mediaId 的低层
descriptor 查询不能替代 API 授权；HTTP 层必须调用关系解析器并再次核对两级关系。
错误只返回统一 code、状态和安全消息，不包含 attachment id、路径或数据库错误。

## 5. 实体状态机

实现定义以下互斥状态：

- `metadata_only`
- `asset_record_missing`
- `asset_record_present`
- `entity_path_missing`
- `entity_missing`
- `entity_outside_allowed_root`
- `entity_symlink_escape`
- `entity_not_regular_file`
- `entity_empty`
- `entity_size_mismatch`
- `entity_hash_mismatch`
- `entity_mime_mismatch`
- `entity_changed_during_check`
- `entity_available`
- `external_url_blocked`
- `unsupported_source`
- `invalid_relationship`

真实生产数据目前只出现：

- `asset_record_missing=940`；
- `entity_missing=172`。

状态不是布尔值。每个失败原因保持可区分且 fail-closed。

## 6. 允许根目录

生产默认允许根固定为：

```text
imports/chatgpt/
```

选择依据是当前真实 ChatGPT 导入结构以及 `asset_file` 的 `.dat` 叶名语义。生产入口
没有接受任意根目录的参数；模块中的 `_allowed_roots` 是私有测试/可信配置 seam，
不得连接 HTTP 参数、数据库字段或前端数据。

门禁快照：

| 项目 | 前后值 |
| --- | --- |
| 文件数 | 14 / 14 |
| 总字节 | 55,212,991 / 55,212,991 |
| 树 SHA-256 | `0000000000000000000000000000000000000000000000000000000000000000` |
| 内容变化 | 无 |
| 可匹配 `.dat` 实体 | 0 |

不扫描其他 `data/` 子目录、整个项目盘、用户目录、系统盘、UNC 或网络共享。

## 7. 路径安全

`validate_allowed_entity_path()` 只接受受控根下的相对 locator，并执行：

- 空值、控制字符和 NUL 拒绝；
- 前后空格拒绝；
- `/` 与 `\` 规范化；
- `.`、`..` 拒绝；
- URL 编码和双重编码 locator 拒绝；
- Windows 盘符拒绝；
- UNC 拒绝；
- `\\?\` 和 `\\.\` 设备路径拒绝；
- 任一 segment 尾随点或空格拒绝；
- NTFS alternate data stream 拒绝；
- 根目录、祖先和候选组件 symlink/reparse/junction 检查；
- `resolve(strict=False)` 后使用大小写安全的 `commonpath` containment；
- 多个允许根同时命中时按歧义关系 fail-closed。

测试覆盖正常相对路径、两种 `..`、编码/双编码、盘符、UNC、设备路径、尾随点、
尾随空格、ADS、大小写、symlink、junction/reparse 及根目录本身为链接的情形。

失败结果不会包含 canonical path 或操作系统错误全文。

## 8. MIME、大小与完整性检查

只对允许根内的候选文件执行只读检查：

1. `lstat/stat` 和重解析点复核；
2. 普通文件检查；
3. 空文件检查；
4. 读取最多 8 KiB 前缀进行 magic 探测；
5. 声明 MIME、内容 MIME 和扩展名框架比较；
6. 附件声明大小与实际大小比较；
7. 可选 SHA-256 流式读取，每次最多 1 MiB；
8. 流式哈希支持取消；
9. 检查前后大小、mtime、设备和 inode 身份比较；
10. 发生变化时归类 `entity_changed_during_check`。

不会把大文件整体装入内存，不生成缩略图，不打开主动内容。Office ZIP 容器和文本 MIME
别名有显式兼容规则；HTML、SVG、JavaScript 等主动类型不能获得安全内联资格。

真实生产根没有实体，因此：

- 实际大小检查：0；
- 内容 MIME 检查：0；
- 实际内容哈希检查：0；
- `entity_available`：0。

13 条附件/资产目录声明大小冲突已单独统计，但在实体缺失时不伪装成实际文件大小验证。

## 9. 外部 URL 策略

本节点支持识别但不访问：

- `http:`
- `https:`
- `file:`
- `data:`
- `blob:`

任何 attachment locator 中出现这些来源均归类 `external_url_blocked`。实现不包含
`urllib.request`、`requests`、`http.client`、`urlopen` 或 socket 访问，不跟随重定向，
不访问 localhost、局域网、云元数据或 Tailnet。

E4 初版建议永久不做代理：只支持受控本地根中已经存在且通过检查的实体。

## 10. 恢复候选分类

聚合恢复报告输出：

- 状态类别及数量；
- 声明 MIME 分布；
- 声明大小区间；
- 稳定原始 ID 数；
- 稳定公开 ID 数；
- 资产目录映射数；
- 附件/资产目录声明大小冲突数；
- 受控导入恢复候选数；
- `entity_available` 数。

不输出原 attachment id、文件名、会话标题、消息正文、路径或 URL。

当前可通过受控导入进一步调查的候选为 172 条：它们已有精确资产目录 locator，但实体
文件不存在。其余 940 条需要先解释为什么没有资产目录行，不能猜测恢复路径。

## 11. 未来 E4-1B 内部契约

主要内部入口：

- `build_media_index(...)`
- `get_media_descriptor(index, media_id)`
- `resolve_media_relationship(...)`
- `inspect_media_entity(index, media_id)`
- `list_recovery_candidates(index)`
- `validate_allowed_entity_path(...)`

成功 descriptor 只包含：

- `mediaId`
- `mediaKind`
- `safeFilename`
- `declaredMimeType`
- `detectedMimeType`
- `totalSize`
- `etagCandidate`
- `lastModified`
- `supportsRangeCandidate`
- `entityState`
- `canOpen`
- `canStream`
- `unavailableReason`

失败对象只包含：

- `errorCode`
- `entityState`
- `safeMessage`

descriptor 不包含 canonical path、原 attachment id、message/conversation id、数据库行、
堆栈或操作系统错误。只有 `entityState=entity_available` 且关系再次验证成功，未来
HTTP 层才可以继续。

## 12. 测试矩阵

E4-1A 新增测试共 46 项，覆盖：

- 稳定 ID、顺序独立、路径独立、非法格式和碰撞；
- attachment/message/conversation 正常、伪造、重复、空值和孤立关系；
- 精确资产目录映射；
- 路径穿越、编码、盘符、UNC、NTFS 特殊路径、大小写、symlink 和 junction；
- 缺失、目录、空文件、大小/MIME/哈希不匹配、检查期间变化；
- entity available、流式哈希和取消；
- 外部 URL blocked 且无网络；
- 公开 descriptor 和错误无内部路径/ID；
- SQLite 内容不变；
- 无媒体路由、无 HTTP 实现、无媒体 store 写入、Service Worker API 网络直通。

结果：

| 验收 | 结果 |
| --- | --- |
| E4-1A 定向测试 | 46/46 通过 |
| 完整 Python 回归 | 866 项通过，4 个既有条件跳过，0 失败 |
| JavaScript 语法 | 通过 |
| Python 语法/静态编译 | 通过 |
| `git diff --check` | 通过 |

独立 Chrome 本机 origin 探针：

- 生产数据库未使用；
- 浏览器请求方法仅 GET；
- 外部请求 0；
- `thumbnails=0`；
- `mediaObjects=0`；
- CacheStorage 只有 12 个应用壳条目；
- `/api/` 条目 0；
- `/api/media` 条目 0；
- page error 0；
- 非预期 console error 0。

## 13. 已知阻塞

1. 固定根内没有一个真实媒体实体；
2. 940 条附件没有资产目录行；
3. 172 条资产映射均缺失 `.dat` 实体；
4. 13 条附件/资产目录声明大小不一致；
5. 没有真实内容 MIME、哈希或 mtime；
6. 没有真实大文件可验收 Range；
7. 没有删除/替换竞态实体；
8. 没有可供 iPhone/PWA 验收的本地实体；
9. E4-1B 尚未定义 HTTP 授权上下文和统一 404/403 策略。

## 14. entity_available

```text
entity_available = 0
```

这不是 E4-1A 失败。E4-1A 的成功标准是准确说明不可用原因；真实结果已经稳定归类为
`asset_record_missing` 或 `entity_missing`，没有把元数据误报为可下载媒体。

## 15. E4-1B 进入条件

进入 E4-1B 前至少需要：

1. 通过受控、独立、可审计的导入过程恢复至少一条真实实体；
2. 恢复过程不得修改生产 SQLite；
3. 实体必须位于固定允许根并通过 reparse/symlink 门禁；
4. attachment、asset 和实际大小冲突必须得到解释；
5. 至少一条安全图片、一条附件和一条大文件 fixture 通过 MIME/大小/哈希检查；
6. 明确 conversation/message 授权上下文；
7. 固定 GET/HEAD-only 和统一无泄漏错误契约；
8. 完成真实 Range、ETag、Last-Modified、nosniff 和 Content-Disposition 测试；
9. DB/WAL/SHM 和实体根指纹继续保持不变。

当前不满足第 1、4、5、6、7、8 项，因此最终判定为：

**BLOCKED — 不进入 E4-1B。**

## 16. 数据库与生产文件门禁

| 文件 | SHA-256 | 大小 |
| --- | --- | ---: |
| DB | `0000000000000000000000000000000000000000000000000000000000000000` | 60,878,848 |
| WAL | `0000000000000000000000000000000000000000000000000000000000000000` | 0 |
| SHM | `0000000000000000000000000000000000000000000000000000000000000000` | 32,768 |

单次最终探针内部前后比较确认三者 SHA-256、大小和 mtime 未变化。节点起点到最终状态
的比较为：

- DB：SHA-256、大小和 mtime 均不变；
- WAL：SHA-256、0 字节大小和 mtime 均不变；
- SHM：SHA-256 和 32,768 字节大小不变；mtime 从
  `2026-07-25T13:02:37.627015Z` 前移到
  `2026-07-25T13:58:24.330291Z`。

SHM mtime 变化发生在完整回归启动既有 real-readonly 服务期间，未产生字节变化。按本
节点明确的内容/大小验收规则，数据库门禁通过，同时保留该元数据触碰记录。允许媒体根
的文件数、总大小、树哈希和内容哈希样本均未变化。没有生产媒体文件被修改、移动、
重命名或派生。

## 17. 生产边界

本节点未修改：

- `tools/local_web.py`
- `web/`
- Service Worker
- IndexedDB schema
- SQLite schema
- 导入器
- Tailscale 配置

当前仍满足：

- 无 `/api/media` 路由；
- 无媒体 GET/HEAD 字节输出；
- 无 PUT/PATCH/DELETE handler；
- Service Worker 对 `/api/` 继续网络直通；
- 应用没有 `thumbnails` 或 `mediaObjects` 写入；
- 不进入 E4-1B；
- 不实现媒体缓存。

## 18. 审计证据

未跟踪证据目录：

```text
output/playwright/v8.2-e4-1a-media-index-recovery/
```

包含：

- `recovery_probe.py`
- `recovery_report.json`
- `browser_storage_gate.js`
- `browser_storage_result.json`

目录保持未跟踪，不暂存、不提交。

## 19. 实现文件

E4-1A 工作区文件：

- `memory/media_index.py`
- `tests/test_v82_media_index_recovery.py`
- `docs/design/yunos_v8_2_e4_1a_media_index_recovery.md`

这些 E4-1A 文件按节点停止规则保持未暂存、未提交。
