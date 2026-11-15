# Local Web UI

AyunOS includes a local Web UI for browsing a sanitized memory database from a Chinese interface. The current version focuses on searchable memory cards, original source references, context review, full conversation review, lightweight project grouping, and local tag/status updates.

If the browser still shows an older interface after an update, use `Ctrl + F5` to force-refresh cached assets.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web
```

Default address:

```text
http://127.0.0.1:8765
```

The default Web UI uses the sanitized test database. To preview the real local memory database without allowing writes, use the explicit read-only real command:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

Readonly real mode prints a warning before startup. The page header shows `数据库模式：真实记忆库（只读）` and displays: `当前正在浏览真实本地记忆库。只读模式已启用，系统不会写入真实数据。`

## Chinese Interface

The visible UI uses Chinese labels for navigation, database mode, buttons, roles, status values, validation messages, and detail views.

- `最近对话` shows recent conversations.
- `搜索记忆` searches titles and message text.
- `项目视图` shows lightweight mock project groups.
- `时间轴` groups conversation activity by month or day.
- `状态管理` lists marked conversations.
- `标签管理` lists local tags.
- `关于系统` describes the local-only boundary.

The page header always shows the active database mode. The default is `数据库模式：测试库`.

In real read-only mode, search, project views, context views, and full conversation detail views also show the read-only notice. Tag and status write controls are disabled and explain that real database writes are forbidden.

Search, Project Hub, and Timeline can filter by source platform. Current supported values are ChatGPT, DeepSeek, Claude, Gemini, manual notes, Markdown, TXT, and unknown source. In this phase, DeepSeek records in the test database are synthetic mock records only.

## Original Source References

An original source reference is a local pointer from a rendered hit or message back to its source conversation and message. The UI uses Chinese action labels such as `原文`, `查看原文`, `定位原文`, `复制引用`, `打开完整对话`, and `命中消息` instead of exposing database fields as the primary concept.

Reference strings use this local format:

```text
ayunos://conversation/mock-conv-0001?message=mock-msg-0001-0004
```

Browsers do not need to open this custom protocol. The Web UI only copies the text so it can be pasted into notes, issues, or future local tools.

In the test database, `原文` means synthetic mock source messages. In real mode, `原文` means the local ChatGPT source messages in the real memory database.

## Search Memory

Open `搜索记忆`, enter a keyword, and press Enter or click `搜索记忆`.

Empty keywords are rejected in the browser with `请输入搜索关键词。` before any request is sent. The search API also returns a JSON error for empty queries.

Use the source selector to search all sources or only one source platform. Search result cards show the source label, such as `来源：ChatGPT`.

The rebuilt test database includes long synthetic conversations. Search `锁鞋` to verify the full source-reference path against the mock conversation `骑行装备预算讨论`. The expected hit mentions cycling shoes, cycling glasses, and a budget of about 3500 yuan.

Search results are displayed as `整理模式` hit cards. Each card shows title, update time, hit source, role label, highlighted snippet, status, tags, `命中消息`, and `原文对话`.

Each hit card supports:

- `查看上下文`: show nearby original messages around the hit.
- `查看原文`: open the full original conversation focused on the hit message.
- `打开完整对话`: open the full conversation.
- `复制引用`: copy the local `ayunos://conversation/...` reference string.

Clicking the snippet text itself is the same as `查看原文`.

## Source Mode

`原文模式` is used for context windows, full conversation detail, and the original source reference panel. It uses a message-flow view instead of hit cards so the user can inspect the source text.

`命中上下文` displays nearby messages before and after the focused hit. It is not a summary. Clicking a context message body opens the full original conversation and focuses that message.

`原文对话详情` displays the full conversation. Its message section is titled `原始消息记录`. Each message has a stable page id in the form `message-<message_id>`, and each message body can be clicked to select and locate that original message.

When a message is selected, the `原文引用` panel shows:

- Current selected message
- Conversation ID
- Message ID
- Chinese role label
- Message timestamp
- Current database mode
- Local reference string

The panel actions are `复制引用`, `查看上下文`, and `返回完整对话`.

## Project Hub

`项目视图` is a lightweight prototype, not a project management system. It shows project cards backed by keyword rules. Project message hits reuse the same search hit cards and source-reference actions.

In the default Web UI, project endpoints read the sanitized test database. In real read-only mode, they read the real local database through the same keyword rules, cap result lists, return snippets rather than saving match results, and do not write data.

Project cards include source distribution counts when more than one source is present. If only ChatGPT is present, the UI can show the compact source label.

Project definitions are file-based. Test and public demo mode reads:

```text
config/projects_public.json
```

Real database mode reads the ignored local file:

```text
config_private/projects_private.json
```

Each project can define `name`, `description`, `status`, `priority`, `keywords`, and `aliases`. Matching uses the project name plus keywords and aliases. `priority` only controls project card order.

Project config files can be checked with:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 validate-projects
```

The real database count check is available through `validate-projects-real`. It runs read-only and prints counts only, not message text or conversation titles.

## Timeline View

`时间轴` shows when memory activity happened. The default grouping is by month. The view can switch to day grouping, filter by project, or filter by a keyword.

Each period card shows conversation count, message count, source distribution, top related projects, and a capped list of conversations. Opening a conversation from Timeline is an explicit original-source action and the conversation page includes `返回时间轴`.

Project detail pages include `查看该项目时间轴`. Search results include a keyword Timeline entry after a successful search.

The `/api/timeline` endpoint returns counts, conversation titles, update times, and project match labels. It does not return full message text or local paths.

## Real Readonly Mode

Use:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

Readonly mode can search, show Project Hub, show Timeline, open context, open full original conversations, focus messages, and copy local references. It cannot add tags, save status, write notes, run mark/tag migrations, create mark/tag tables, or modify the database.

Write APIs such as `/api/conversation/tag` and `/api/conversation/mark` return HTTP 403 JSON with `Readonly mode: writes are disabled`.

## Health Check

The Web UI exposes a read-only health endpoint:

```text
GET /api/health
```

It returns aggregate status fields, including `source_trace_enabled`, `project_hub_enabled`, `timeline_enabled`, `long_mock_scenarios_enabled`, and `ui_language`. It does not return local paths, titles, or message bodies.

## Search 模式的本地边界

以下边界只描述 Omnibox 的 Search 模式及其本地只读查询接口，不代表独立的 AI 模式没有
Provider 能力。v10 不会把 Search 偷偷升级成 AI 搜索。

- Search 服务默认绑定 `127.0.0.1`，并拒绝 `0.0.0.0`。
- Search 不上传查询或结果，不调用外部 API/Provider，不产生模型费用，也不要求网络连接。
- Search 不生成 AI 摘要，不连接 Ollama；现有 SQLite/FTS、项目、时间轴和来源定位链保持本地。
- Search 不把离线缓存伪装成实时状态。
- 默认模式使用 sanitized test database；真实数据库只读预览必须显式选择
  `web-real-readonly`。
- Tailnet 与移动端只能使用只读接口，不能借反向代理获得本机写权限。

AI 模式是独立链路：它只在明确的 Objective、上下文预算和手动 Provider 选择下发送最小必要
内容，并通过 v8 Runtime 记录调用。AI 的 tool proposal 不等于执行权限；任何 Act 写入仍须
经过独立 Authorization Artifact 与不暴露给 Tailnet 的本机受控入口。
