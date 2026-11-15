# YunOS v5 Memory Lifecycle Seal

YunOS v5 is complete. The lifecycle is intentionally controlled and summary-only.

## Delivered stages

- **v5-A/A.1 Planner:** read-only classification of `up_to_date`, missing, stale, identity mismatch, and legacy states; current compatible embeddings take priority over legacy records. The real read-only acceptance found two summaries, both up to date.
- **v5-B Executor:** dry-run by default; only `create_embedding` and `rebuild_embedding`; idempotent, append-only maintenance without deleting older embeddings.
- **v5-C Orchestrator:** deterministic `plan_id`, immediate replanning before execution, stale or incorrect confirmation rejection, and post-execution verification.
- **v5-D Production sidecar:** writes are isolated to `data/semantic/ayunos_semantic.db`; the real chat database remains read-only and production writes require explicit confirmation.
- **v5-E Transactional maintenance:** one transaction per plan, all-or-nothing rollback, pre-write sidecar backup, fast `maintenance_busy` on concurrent maintenance, and auditable results.
- **v5-F One-shot entrypoint:** `check`, `prepare`, and explicitly confirmed `execute`; missing sidecars are never initialized automatically and zero-action plans do not open write transactions or create backups.

## Permanent safety boundary

Lifecycle maintenance does not read `messages`, create or modify summaries, call external APIs or LLMs, connect to whale_chat/Web, delete historical embeddings, or add background scheduling. Defaults are read-only or dry-run; every production write requires explicit confirmation.

## Deferred technical debt

Natural-query keyword gaps, hybrid cross-source deduplication/fusion bias, and broader de-identified evaluation coverage are explicitly outside the v5 seal.
