# AyunOS Safe Git Workflow

AyunOS treats imported ChatGPT material as local-only private data. Source code and project documentation can be committed, but raw exports, generated databases, and personal conversation records must stay on this machine.

## Never Commit

- `data/`
- `imports/`
- `backups/`
- SQLite databases, including `ayunos_chatgpt.db`
- Raw message exports, including `messages.jsonl`
- ChatGPT export packages, including `ayunos_chatgpt_import_pack.zip`
- Generated indexes such as `conversations_index.csv` and `assets_index.csv`
- Cache, build, virtualenv, and editor folders such as `.venv/`, `venv/`, `node_modules/`, `dist/`, and `build/`

## Safe To Commit

- Project source code under `src/`, once it exists
- Local tools under `tools/`, as long as they do not contain real private records
- General project docs under `docs/`, as long as they describe architecture, workflow, or schemas without exposing private chat content
- `README.md`
- `.gitignore`

## Why Private Data Stays Local

The `data/`, `imports/`, and `backups/` folders may contain complete databases, raw ChatGPT exports, copied assets, and recovery snapshots. These files can include private conversations, personal plans, health details, family topics, financial notes, and other sensitive context. They are useful for local memory search, but they should never become GitHub history.

## Required Pre-Add Check

Before every broad staging command such as `git add .`, run:

```powershell
python E:\AyunOS\tools\privacy_scan.py
```

If `python` is not available on PATH, run the same script with the local Python interpreter you use for AyunOS development. Treat any `PRIVACY SCAN WARNING` as a stop sign: inspect the listed files before staging.

## External Tool Boundary

Do not upload complete databases, `messages.jsonl`, or ChatGPT export packages to GitHub. Lovable and Canva workflows may use mock data only. They must not receive real chat records, raw exports, private database files, or copied conversation text.
