# Project Config Validation

AyunOS project config validation checks the file-based Project Hub definitions before they are used by the local Web UI. It is a configuration safety tool only. It does not add product features, summarize content, call AI services, build vectors, or modify databases.

## Run Public Test Validation

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 validate-projects
```

This validates the public project config and, when present, the ignored private project config against the sanitized test database.

## Run Real Readonly Validation

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 validate-projects-real
```

This validates the project config against the real local memory database in read-only mode. It prints counts only. It does not print message text or conversation titles.

## Result Meanings

- `AYUNOS PROJECT CONFIG VALIDATION PASSED`: no errors or warnings were found.
- `AYUNOS PROJECT CONFIG VALIDATION WARNING`: validation ran, but some project definitions may need attention.
- `AYUNOS PROJECT CONFIG VALIDATION FAILED`: validation found a blocking config problem, such as invalid JSON, a non-array top level, missing required fields, duplicate project names, empty keywords, or field type errors.

Warnings include short or generic keywords, shared keywords or aliases across projects, zero matches, unusually broad matches, and a missing private config.

## Public And Private Configs

Public test/demo project config:

```text
config/projects_public.json
```

Private real-mode project config:

```text
config_private/projects_private.json
```

The public config is safe to commit and is used by the sanitized test database workflow. The private config stays local because it may encode personal project routing preferences. `config_private/` is ignored and must not be committed.

## Safety Boundary

The validator:

- opens SQLite databases read-only;
- does not write the test database;
- does not write the real database;
- does not upload data;
- does not call external APIs;
- does not connect to Ollama;
- does not generate AI summaries;
- does not use a vector database;
- prints only project summaries and match counts;
- does not print real message text or real conversation titles.
