# YunOS v5.0.1 Execution Hardening

v5.0.1 closes the three production execution blockers identified after the v5 seal. It does not add lifecycle features or broaden data access.

## Confirmed plan envelope

Production execution now requires an immutable, versioned plan envelope. The canonical plan ID binds `target_kind`, complete provider identity, sorted summary IDs, memory spaces, actions, and content hashes. The Orchestrator replans before execution, and the Executor revalidates the envelope and rereads each summary hash before backup or transaction entry. A production boolean flag alone cannot authorize a write.

The confirmed envelope contains no summary text, vector, or absolute path. The internal execution representation uses only `action`; legacy `recommended_action` remains confined to the read-only Planner boundary.

The production gateway revalidates the complete confirmed envelope immediately before maintenance. `save_embedding_record` cannot write the production sidecar directly, and the records-level transaction entry is private. Executor stale results use the same complete audit shape. These private boundaries are not claimed to resist malicious same-process reflection.

## Strict transaction semantics

A production plan uses one SQLite connection and one `BEGIN IMMEDIATE` transaction. Only `SQLITE_CONSTRAINT_UNIQUE` and `SQLITE_CONSTRAINT_PRIMARYKEY` are duplicates. NOT NULL, CHECK, foreign-key, trigger, schema, I/O, and other failures roll back the batch. Only `SQLITE_BUSY` and `SQLITE_LOCKED` produce `maintenance_busy`. Historical embeddings remain append-only.

## Status, backup, and audit consistency

`committed`, `rolled_back`, `maintenance_busy`, `failed`, `refused`, `stale_plan`, and `no_action` retain consistent meanings across Executor, Orchestrator, CLI JSON, backup metadata, and audit output. The user-confirmed plan ID is reused throughout. Backup names include UTC microseconds, the plan prefix, and run ID; tests verify backup hash, SQLite readability, and restoration into a temporary database.

Zero-action, refused, and stale plans do not create backups or open write transactions. One-shot check and prepare remain read-only. Audit output excludes source text, vectors, absolute paths, and exception stacks; audit-file failure does not change the database transaction result.

## Safety boundary and evidence

Lifecycle maintenance does not read raw messages, create or modify summaries, call external APIs or LLMs, connect to whale_chat or Web, delete historical embeddings, or add background automation. The real chat database remains read-only and production writes remain isolated to the semantic sidecar.

Targeted tests cover confirmed-envelope tampering and TOCTOU rejection, strict duplicate/error classification, whole-batch rollback, real SQLite busy behavior, status propagation, plan ID consistency, backup uniqueness, and temporary restore. These controlled tests reduce known execution risk but do not by themselves establish production quality under every hardware or failure condition.

Gateway tests explicitly close source, sidecar, verification, and test-database SQLite connections before temporary cleanup; production connection lifetimes are likewise explicit.
