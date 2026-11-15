# Test Database Policy

AyunOS separates the development database from the real local memory database. This keeps everyday development safe and prevents test tags, review states, and UI smoke checks from touching real records by accident.

## Default Database

Default commands use the sanitized test database:

```text
data/test/ayunos_memory_test.db
```

The test database contains mock titles, mock message text, and mock ids. It does not contain real titles, real message bodies, real author names, real metadata, or a real timeline.

The test database also includes `source_platform` columns on `conversations` and `messages`. Most records are `chatgpt`; a small number of records are synthetic `deepseek` and `manual` mock records so the Web UI can validate source filtering. No real DeepSeek data is imported in this phase.

## Long Mock Scenarios

The default generated test database includes long synthetic conversations for Web UI and Source Trace testing:

- `骑行装备预算讨论`
- `AyunOS 第二大脑设计讨论`
- `专升本复习计划`
- `ComfyUI 调试记录`
- `写作项目归档`

These conversations are synthetic. They use `mock-conv-xxxx` conversation ids, `mock-msg-xxxx-yyyy` message ids, mock timestamps, empty author names, `[]` children JSON, and `{}` metadata JSON.

Use search term `锁鞋` to verify the cycling Source Trace scenario. The expected hit is `骑行装备预算讨论`, and the test snippet should mention cycling shoes, cycling glasses, and about 3500 yuan.

## Real Database Access

The real local memory database is still supported, but it must be selected explicitly. The preferred Web UI path is read-only preview mode:

Example:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

This mode opens the real database with writes disabled. It supports search, Project Hub, context review, full original conversations, focused messages, and copying local references. It does not add tags, save status, write notes, create mark/tag tables, run migrations, or modify the database.

Real read-only mode prints a warning before running and the page shows `数据库模式：真实记忆库（只读）`.

## Rebuilding The Test Database

The test database can be rebuilt whenever needed:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 create-test-db --overwrite
```

The generator reads the real database in read-only mode, copies only the table shape needed for local testing, and fills it with mock content.

If the source database does not have `source_platform`, the generated test database adds the column locally and fills it with mock source values. This does not alter the real database.

If the test database already exists, `--overwrite` is allowed only when the target is the test path under `data/test/ayunos_memory_test.db`. Never use overwrite against the real local database.

If a browser tab or local Web UI process is using the test database, stop that local test process before rebuilding.

## Development Rule

Use the test database for development, UI testing, searches, tags, marks, and review workflows. Do not use the real local memory database for routine development testing.

The Web UI should default to the test database. Real database preview must use the explicit `web-real-readonly` command. Browser cache can make an old UI appear after local changes; use `Ctrl + F5` when validating a refreshed Web UI.

## Public Project Configuration

Project Hub definitions for the test database live in:

```text
config/projects_public.json
```

This tracked file is safe for public demos and should contain only synthetic or generic project labels. To add or remove a public demo project, edit the JSON file and refresh the Web UI; no Python code change is required.

The private real-database project file lives under ignored `config_private/` and must not be committed.
