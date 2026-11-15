# AyunOS Security Audit

This document describes the public engineering and safety audit for AyunOS. It is for local development only and must not include real chat text, real conversation titles, real usernames, private local paths, private project details, or database contents.

## Audit Scope

- Git tracked-file boundary for private folders, databases, exports, logs, and archives.
- `.gitignore` coverage for private data, private config, virtual environments, databases, JSONL exports, and zip files.
- Default command boundary for test database usage.
- Explicit real-database commands and the `web-real-readonly` read-only boundary.
- Web API response safety, including path redaction, snippet limits, bounded context windows, sanitized errors, and readonly POST rejection.
- Frontend safety, including readonly warnings, disabled write controls, and absence of hidden write entry points.
- Project configuration boundary between public mock config and ignored private config.
- Public documentation and demo privacy scan.
- Real database source-table health by readonly row counts only.

## How To Run

Run the local verification sequence from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 smoke
powershell -ExecutionPolicy Bypass -File .\run.ps1 scan
powershell -ExecutionPolicy Bypass -File .\run.ps1 audit
```

For manual Git boundary checks:

```powershell
git status --ignored --short
git ls-files
```

For real database write-protection checks, start the readonly web server only:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 web-real-readonly
```

Then verify that POST requests to write APIs return HTTP 403 and that source-table row counts stay unchanged. Do not print real titles or message text.

## Result Levels

`AYUNOS AUDIT PASSED` means no failures or warnings were found.

`AYUNOS AUDIT WARNING` means no blocking failure was found, but a condition needs human awareness or later cleanup.

`AYUNOS AUDIT FAILED` means a safety boundary failed and the result must not be treated as shippable until fixed.

## Current Known Warning

- The real local database may contain historical test or smoke-test markers in local mark/tag tables. This is a WARNING because it affects local metadata hygiene, not source `conversations` or `messages` table integrity.

## Current Safety Boundary

- Public source and docs may describe architecture and safety rules, but must not include real chat content.
- `data/`, `imports/`, `backups/`, `logs/`, `docs_private/`, `config_private/`, and `.venv/` are local-only and ignored.
- Databases, SQLite sidecars, JSONL exports, and zip archives are local-only and ignored.
- `config/projects_public.json` is public mock configuration.
- `config_private/projects_private.json` is ignored private configuration and must not be committed.
- Test-mode commands use the sanitized test database by default.
- Real-mode commands must be explicit and visibly warned.

## Real Database Readonly Principle

The real local memory database is only inspected through readonly boundaries during this audit. `web-real-readonly` must pass `--real --readonly`, `local_web.py` must open SQLite with `mode=ro`, readonly mode must skip mark/tag migrations, and all readonly POST requests must return HTTP 403.

Real database validation may report source-table row counts and marker counts only. It must not output titles, message text, private paths, private project details, or raw records.

## Network And API Boundary

This audit does not use network access, upload files, call external APIs, connect to Ollama, run AI summarization, or write to the real database.
