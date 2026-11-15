# Real Database Readonly Mode

AyunOS keeps the real local memory database in read-only preview mode during this phase. This allows source review against real local records without risking accidental tags, status changes, notes, migrations, or schema writes.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

The command opens the real local memory database explicitly and starts `tools/local_web.py` with `--real --readonly`.

The default Web UI command remains the sanitized test database:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web
```

## Allowed In Readonly Mode

- Search memory records.
- Open Project Hub and project detail views.
- Open Timeline by month or day.
- Filter Timeline by project or keyword.
- Filter Search, Project Hub, and Timeline by source platform.
- View hit context.
- Open full original conversations.
- Focus a specific original message.
- Copy local `ayunos://conversation/...` references.

Project Hub reads ignored local project definitions from:

```text
config_private/projects_private.json
```

The file is a JSON array. Each project may include `name`, `description`, `status`, `priority`, `keywords`, and `aliases`. Matching uses the project name plus keywords and aliases. This file stays local and must not be committed.

Project config can be validated against the real local database without writes:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 validate-projects-real
```

The validator prints project match counts only. It does not print real message text or real conversation titles.

Timeline View is also available in readonly mode. It returns period counts, project match labels, conversation titles, update times, and message counts. It does not return full message text or local paths from `/api/timeline`.

The real local memory database may not have a `source_platform` column. In that case, the Web UI treats those rows as ChatGPT at read time. It does not alter the real schema. Filtering for DeepSeek can return an empty result without causing an error.

## Disabled In Readonly Mode

- Add tags.
- Save status.
- Write notes.
- Create mark/tag tables.
- Run mark/tag migrations.
- Modify the database.

All POST write APIs return:

```json
{
  "error": "Readonly mode: writes are disabled"
}
```

with HTTP 403.

## Local Boundary

Readonly mode does not upload data, does not call external APIs, does not connect to Ollama, does not use a vector database, and does not generate AI summaries. It is a local browser preview over `127.0.0.1`.

Public source and documentation must not include real conversation titles, real message text, personal identifiers, raw exports, generated databases, or private paths.
