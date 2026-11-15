# Timeline View

AyunOS Timeline View answers a different question from Project Hub.

Project Hub asks: what am I thinking about?

Timeline View asks: when was I thinking about what?

The view groups conversations by month or day, shows activity counts, highlights related project matches, and lets the user open the original conversation on purpose.

## Open Timeline

Start the local Web UI:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web
```

Then open the `时间轴` navigation item.

The default view groups by month.

## Group By Month

Use `分组方式：按月` to show activity buckets such as `2026-06`.

Each bucket shows:

- conversation count;
- message count;
- source distribution;
- top related projects;
- up to 20 conversations for that period.

## Group By Day

Use `分组方式：按日` to show daily activity buckets such as `2026-06-10`.

This is useful when a month has many conversations and the user wants a more precise activity rhythm.

## Filter By Project

Use `项目筛选` to show only conversations matching one configured project.

Project filtering reuses the file-based Project Hub config. It does not write match results to the database.

Project detail pages also include `查看该项目时间轴`, which opens Timeline with that project filter already selected.

## Filter By Keyword

Use `关键词筛选` for simple title/text matching.

Search results also include `查看“关键词”的时间轴`, which opens Timeline with that keyword filter already selected.

This is plain SQLite matching. It does not call AI, does not summarize, and does not use a vector database.

## Filter By Source

Use `来源筛选` to show all sources or one source platform, such as ChatGPT, DeepSeek, Claude, Gemini, manual notes, Markdown, or TXT.

Each period can show a source distribution such as `来源：ChatGPT 8，DeepSeek 1`. In this phase, DeepSeek records in the test database are synthetic mock records only.

## Jump To Original Conversation

Timeline conversation rows include `打开原文`. Clicking a row title also opens the full original conversation.

When a conversation is opened from Timeline, the detail page shows `返回时间轴` so the user can return to the same Timeline filter.

## Real Readonly Mode

Timeline is available in real database read-only mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

Readonly mode can view Timeline and open original conversations by explicit user action. It cannot write tags, save status, create tables, migrate data, or modify the real database.

## Local Boundary

Timeline validation and browsing:

- does not upload data;
- does not call external APIs;
- does not connect to Ollama;
- does not generate AI summaries;
- does not use a vector database;
- does not write the real database;
- does not return full message text from `/api/timeline`;
- does not return real local paths from `/api/timeline`.
