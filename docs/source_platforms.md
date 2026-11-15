# Source Platforms

AyunOS now has a source platform field as the foundation for multi-source memory. The goal is to know where a memory record came from before future importers add more data sources.

This stage only adds schema, API, UI, documentation, and audit support. It does not import DeepSeek data and does not modify the real local memory database.

## Supported Values

- `chatgpt`: ChatGPT
- `deepseek`: DeepSeek
- `claude`: Claude
- `gemini`: Gemini
- `manual`: manual notes
- `markdown`: Markdown files
- `txt`: TXT files
- `unknown`: unknown source

## Current Behavior

The existing ChatGPT memory data is treated as:

```text
source_platform=chatgpt
```

The generated test database includes mostly `chatgpt` synthetic records plus a small number of synthetic `deepseek` and `manual` records for UI testing. These are mock records only.

The real local memory database may not have a `source_platform` column yet. When the column is missing, AyunOS treats those rows as `chatgpt` at read time. It does not alter, migrate, or write the real database.

## Future Importers

Future DeepSeek import code should write:

```text
source_platform=deepseek
```

The same pattern should be used for Claude, Gemini, Markdown, TXT, and manual notes. Importers should set the field during ingestion instead of relying on fallback behavior.

## UI Filtering

The Web UI can filter by source platform in:

- Search
- Project Hub
- Timeline

When no source is selected, the UI shows all sources. In real read-only mode, selecting DeepSeek can return an empty result if no DeepSeek records exist.

Project Hub and Timeline also show source distributions such as:

```text
来源：ChatGPT 8，DeepSeek 1
```

## Local Boundary

Source platform support:

- does not upload data;
- does not call external APIs;
- does not connect to Ollama;
- does not generate AI summaries;
- does not use a vector database;
- does not import DeepSeek data in this stage;
- does not write the real local memory database.
