# Memory Browser MVP

This MVP is a local command-line browser for an imported conversation memory database. It helps review recent conversations, search local records, add lightweight tags, and mark conversations for later work.

The default development database is the sanitized test database at `data/test/ayunos_memory_test.db`. Use explicit `*-real` runner commands only when you intentionally want to inspect the real local memory database.

## Current Capabilities

- Back up the local SQLite database before changes.
- Add local mark and tag tables without changing source conversation or message tables.
- View recent conversations from the command line.
- Search conversation titles and message text.
- Open a conversation detail view with long-message truncation.
- Add tags to conversations.
- Mark conversations with one of these statuses: `keep`, `trash`, `project`, `study`, `writing`, `life`, `tech`, `review`.
- List marked conversations and list conversations under a tag.

## Back Up The Database

Recommended local runner:

```powershell
.\run.ps1 backup
```

This backs up the test database by default. Use `.\run.ps1 backup-real` only when you intentionally need a real local database backup.

If Windows blocks direct `.ps1` execution, use a one-time process policy override:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 backup
```

Direct script call:

```powershell
python .\tools\backup_memory_db.py
```

Optional arguments:

```powershell
python .\tools\backup_memory_db.py --db <PROJECT_ROOT>\data\local_memory.db --out-dir <PROJECT_ROOT>\backups
```

## Run The Migration

Recommended local runner:

```powershell
.\run.ps1 migrate
```

Direct script call:

```powershell
python .\tools\migrate_memory_marks.py
```

Optional database path:

```powershell
python .\tools\migrate_memory_marks.py --db <PROJECT_ROOT>\data\local_memory.db
```

The migration only creates local mark/tag tables and indexes if they do not already exist.

## Run The Interactive Browser

Recommended local runner:

```powershell
.\run.ps1 browser
```

Use `.\run.ps1 browser-real` only for explicit real database inspection.

Direct script call:

```powershell
python .\tools\memory_browser.py
```

Optional database path:

```powershell
python .\tools\memory_browser.py --db <PROJECT_ROOT>\data\local_memory.db
```

## Run Read-Only Search

Recommended local runner:

```powershell
.\run.ps1 search "example query"
```

Use `.\run.ps1 search-real "example query"` only for explicit real database search.

Direct script calls:

```powershell
python .\tools\search_memory.py "example query"
python .\tools\search_memory.py "example query" --limit 5
python .\tools\search_memory.py "example query" --db <PROJECT_ROOT>\data\local_memory.db
```

## Current Boundaries

- No network access.
- No cloud API calls.
- No AI summaries.
- No vector database.
- No web UI yet.
- Public documentation and source code should use placeholders or mock data only.
