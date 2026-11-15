# Project Audit

The AyunOS project audit is a local, read-only engineering check for repository safety and database integrity.

## Purpose

- Confirm Git tracked files do not include private data artifacts.
- Confirm the local memory database can be opened read-only.
- Confirm source conversation and message tables still exist.
- Confirm local mark and tag tables exist and report their row counts.
- Warn when local test tags or smoke-test notes are present.
- Confirm basic safety files and ignored directories are configured.

## How To Run

```powershell
.\.venv\Scripts\python.exe tools\audit_project.py
```

Or, if using the local runner:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 audit
```

The default audit checks the sanitized test database. To inspect the real local memory database explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 audit-real
```

## Safety Boundary

The audit does not access the network. It does not upload anything. It does not call external APIs. It does not read message body text. It does not print real conversation titles. It does not modify the database.
