# YunOS v3 Memory Summarizer Seal

Date: 2026-07-05

This document records the sealed state of the YunOS v3 Memory Summarizer layer.
It is a status and safety note only. It does not introduce new behavior.

## Completed Scope

- v3-A: Memory Summarizer scaffold.
  - Added the summary table design.
  - Added extractive summary builders, dry-run behavior, CLI scaffolding, and tests.
- v3-B: Controlled test database write.
  - Enabled summary migration and writes only for test databases and temporary SQLite files.
  - Refused production database writes.
- v3-C: Real database safe append.
  - Added explicit backup and confirmation gates for real database summary append.
  - Kept real writes restricted to whale summaries with duplicate protection.
- v3-D: Read-only summary viewer.
  - Added read-only summary listing.
  - Added GET `/api/memory-summaries`.
  - Added Web display for existing summaries.
- v3-E: Summary health diagnostics.
  - Added read-only health scoring and warning diagnostics.
  - Added GET `/api/memory-summary-health`.
  - Added Web display of health score and warning state.
- v3-F: whale_chat summary context injection.
  - Added read-only injection of whale `memory_summaries` into prompt assembly.
  - High-level summaries are placed before raw retrieved whale memory.

## Current Real Database State

The current real local database state at v3 seal:

- `messages`: 5917
- `messages` where `source_platform='deepseek'` and `memory_space='whale'`: 688
- `memory_summaries`: 2
- Summary method: `extractive_v1`

The current sealed summary rows are whale-scoped:

- `memory_space_overview`
- `conversation_summary`

## Safety Boundaries

YunOS v3 keeps these boundaries:

- It does not automatically rewrite original `messages`.
- It does not automatically call any LLM, DeepSeek API, or provider API.
- It does not automatically refresh or regenerate summaries.
- `whale_chat.py` reads summaries in read-only mode as high-level context only.
- `memory_space` remains separated into `whale` and `general`.
- whale flows do not silently broaden to all local history.
- general summaries are not automatically injected into whale chat.
- Raw exports, private databases, logs, backups, and real chat records remain local-only.

## Prompt Context Order

The sealed whale prompt order is:

1. `[System]`
2. `persona_whale.md` content
3. `[High-level Memory Summaries - whale]`
4. `[Relevant Memory - whale]`
5. `[Optional Context - general]`, only when the existing optional-general logic asks for it
6. `[User Input]`

## Verification Set

The v3 seal should be verified with:

```powershell
python tools/privacy_scan.py
python tools/audit_project.py
python tools/smoke_test.py
python -m unittest tests.test_memory_summarizer tests.test_memory_summary_reader tests.test_memory_summary_health tests.test_whale_chat_summary_context tests.test_deepseek_importer tests.test_audit_project tests.test_v2_memory_system -v
```

If `audit_project.py` reports only the known historical test-marker warning, the v3 seal can continue without cleaning or rewriting the real database.

## Next Stages

Recommended next stage:

- v3.1: Prompt/context inspector.
  - Show which prompt sections were included.
  - Show character or token estimates per section.
  - Keep it read-only and do not call provider APIs.

Reserved future stage:

- v4: Higher-quality LLM summary generation or semantic retrieval.
  - Do not implement this in v3-final.
  - Any LLM/API summary flow should require explicit user approval, dry-run review, and safety gates.
