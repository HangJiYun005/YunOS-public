# ChatGPT Importer Public Notes

The ChatGPT importer converts a local ChatGPT export archive into local search artifacts for AyunOS. It is designed for private, offline use: raw exports and generated data stay under the project data/import folders and are not committed.

## Purpose

- Read a ChatGPT export zip from a local path such as `<PROJECT_ROOT>\imports\chatgpt\export.zip`.
- Parse conversation metadata, messages, and asset references.
- Write local outputs under a private data directory such as `<PROJECT_ROOT>\data\chatgpt_export`.
- Produce a SQLite database, CSV indexes, JSONL messages, and optional Markdown summaries for local review.

## Database Shape

The generated SQLite database contains these main tables:

- `conversations`: one row per conversation, with title, timestamps, message counts, tags, and summary metadata.
- `messages`: one row per message, linked to `conversations` by conversation id.
- `assets`: optional asset metadata from the export package.
- `messages_fts`: full-text search index for local lookup.

Example local database path:

```text
<PROJECT_ROOT>\data\chatgpt_export\chatgpt_memory.sqlite
```

## Example Run

```powershell
cd <PROJECT_ROOT>
python .\tools\chatgpt_export_importer.py `
  ".\imports\chatgpt\export.zip" `
  --out ".\data\chatgpt_export"
```

Use `--no-full-text` when creating a preview that should omit message body text from the generated message records.

## Commit Safety

Do not commit generated data, raw exports, or local reports. Public docs should use placeholders and mock examples only.
