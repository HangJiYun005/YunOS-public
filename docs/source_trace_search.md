# Source Trace Search

Source Trace Search helps AyunOS move from database-style search output to source-backed memory review. In P1.3, the user-facing concept is `原文引用`: every useful hit should identify its source conversation, source message, and local reference string.

## Purpose

Search snippets are useful for locating a possible match, but a snippet is not enough for review. Source Trace keeps the workflow grounded in original text:

1. Search a keyword.
2. Read a `整理模式` hit card.
3. Click the snippet or `查看原文`.
4. Open the full original conversation with the focused message highlighted.
5. Use the `原文引用` panel to copy the local reference or inspect nearby context.

This keeps the system useful without generating AI summaries or replacing the source material.

The default test database includes long synthetic conversations for Source Trace validation. The main end-to-end check is:

```text
Search 锁鞋 -> click hit snippet -> open 骑行装备预算讨论 -> focused original message -> copy ayunos:// reference
```

The expected mock hit includes cycling shoes, cycling glasses, and a budget of about 3500 yuan. This content is synthetic and is not copied from a real conversation.

## Hit Cards

`GET /api/search?q=<keyword>` returns search items that can be rendered as readable hit cards. The UI displays the title, updated time, hit source, role label, snippet, status, tags, `命中消息`, `原文对话`, and source-reference actions.

Required item fields:

- `conversation_id`
- `message_id`
- `title`
- `source`
- `role`
- `role_label`
- `snippet`
- `update_time_iso`
- `reference_uri`

Example reference:

```text
ayunos://conversation/mock-conv-0001?message=mock-msg-0001-0004
```

If the keyword is empty, the browser shows `请输入搜索关键词。` and does not send the request. The API still returns a 400 JSON error for empty `q`.

For the P1.3 test database, `GET /api/search?q=锁鞋` should return `骑行装备预算讨论`. A useful hit snippet should mention `锁鞋`, `骑行眼镜`, and `3500`.

## Message Context API

```text
GET /api/message-context?message_id=<message_id>&window=3
```

The API finds the message by `message_id`, reads its `conversation_id`, and returns the focused message plus nearby messages from the same conversation.

Behavior:

- `window` defaults to `3`.
- `window` must be between `0` and `10`.
- Missing `message_id` returns 400.
- Unknown `message_id` returns 404.
- Invalid `window` returns 400.
- The endpoint is read-only.
- Tracebacks are not exposed to the browser.

Each returned message includes:

- `conversation_id`
- `message_id`
- `role`
- `role_label`
- `create_time_iso`
- `text`
- `is_focus`
- `reference_uri`

## Conversation Focus API

```text
GET /api/conversation?id=<conversation_id>&focus_message_id=<message_id>
```

The conversation endpoint returns the full conversation and marks each message with `is_focus`. If `focus_message_id` belongs to the conversation, the UI highlights and scrolls to it. If it does not belong to the conversation, the endpoint still returns the ordinary conversation without failing.

The response includes:

- `conversation.conversation_id`
- `conversation.title`
- `conversation.update_time_iso`
- `conversation.message_count`
- `conversation.focus_message_id`
- top-level `focus_message_id`
- `messages[]`

Each `messages[]` item includes `conversation_id`, `message_id`, `role`, `role_label`, `create_time_iso`, `text`, `is_focus`, and `reference_uri`.

When the UI opens a full conversation from search, it shows `返回搜索结果`. When it opens a full conversation from a context view, it shows `返回上下文`.

## Message-Level Actions

The full conversation view is titled `原文对话详情`, and the message list is titled `原始消息记录`.

Each message in the full conversation view has these actions:

- `查看上下文` calls `/api/message-context` with that message as the focused message.
- `复制引用` copies the local `ayunos://conversation/...` reference.
- `定位此消息` scrolls to that message, highlights it, updates the current selected source reference, and updates the browser hash.

Clicking a message body performs the same source-location behavior. Context messages also support source location by opening the full conversation focused on that message.

These actions do not modify source conversation or message rows.

## Real Readonly Preview

Source Trace also works in real database read-only preview mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

In this mode the UI may search real local records, open hit context, open the full original conversation, focus a matched message, and copy the local reference string. Tag and status writes are disabled. POST write APIs return HTTP 403 JSON with `Readonly mode: writes are disabled`.

## Privacy Boundary

The default Web UI uses the sanitized test database. In the test database, `原文` means mock original messages. Real database preview must be selected with the explicit `web-real-readonly` runner command.

Source Trace Search does not upload data, does not call external APIs, does not perform AI summaries, does not connect to Ollama, and does not use a vector database. Public documentation should use mock identifiers and synthetic examples only.

Use `Ctrl + F5` in the browser if cached assets make the UI appear out of date after a local update.
