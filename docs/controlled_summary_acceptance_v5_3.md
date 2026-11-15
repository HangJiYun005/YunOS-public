# YunOS v5.3 Controlled Summary Acceptance

v5.3 is a controlled bridge from the read-only v5.2 candidate preview to a
versioned formal-summary sidecar. It does not rewrite the original chat
database, initialize the production sidecar automatically, accept a real
candidate during verification, or generate embeddings.

## Storage and confirmation boundary

Formal accepted summaries are stored only in `data/memory/ayunos_memory.db`.
The sidecar is append-only: each `(conversation_id, version)` and
`candidate_id` is unique; historical rows are not updated or deleted. The
original chat database remains read-only. A production sidecar initialization
requires all explicit initialization flags and is outside the normal prepare
path.

`prepare` rereads the source and regenerates one candidate. Its deterministic
plan binds the target kind, candidate and schema IDs, conversation and memory
space, reason, source snapshot/count/timestamps/last ID, generator identity and
configuration hash, candidate text hash, and base-summary ID/hash. It emits
metadata only by default and creates no sidecar, backup, or audit log.

`execute` requires `candidate_id`, `acceptance_plan_id`, and the complete
production confirmation set. Test source and sidecar arguments are rejected if
their file identity matches the chat database, semantic sidecar, or memory
sidecar, or if any existing path component is a symlink or Windows reparse
point. This includes hard links and non-canonical aliases; string comparison
and `Path.resolve()` are not the production boundary.

Execution holds the source read-only against concurrent writable opens on
Windows and serializes supported sidecar writers with a per-sidecar system
mutex. Under that protocol it reacquires `BEGIN IMMEDIATE`, regenerates the
candidate, and rechecks the generator configuration and base summary before
the append. Any source, candidate, generator, base-summary, or plan change
returns `stale_plan`; lock contention returns `source_busy` or `sidecar_busy`.
A repeated candidate returns `already_accepted` only after its stored plan ID
matches the supplied plan. Stale, refused, busy, and repeated requests create
no backup. A successful append creates a SQLite backup first, commits one
version, and rolls back the whole transaction on failure.

## Change detection

v5.1 change detection reads the latest accepted sidecar version first, then
legacy `memory_summaries`, then no summary. A sidecar record is `up_to_date`
only when message coverage and the complete `source_snapshot_hash` match the
current source. Added messages are `new_messages`; equal counts with a changed
snapshot hash are `source_changed`. Legacy rows without reliable coverage
remain `coverage_unknown`. Outputs contain source identifiers, status,
`summary_source`, and `summary_version`, never summary text, candidate text,
message content, or absolute paths.

## Verification boundary

The accepted test path covers missing summary to candidate to prepare to a
temporary sidecar write, followed by `up_to_date`, `new_messages`, and
`source_changed`. It also covers production-file hard links and reparse
aliases, normal system-temporary databases, source and sidecar contention,
base-summary changes, plan-before-idempotency validation, rollback, connection
release, legacy fallback, and latest-version selection.

The final suite completed 303 tests with 2 existing Windows permission skips;
Node syntax, privacy, audit, smoke, and diff checks passed. The final real-data
check remained limited to a read-only prepare for one `summary_missing`
conversation and an execute call missing confirmations. It did not initialize
`data/memory/ayunos_memory.db`, accept a real candidate, modify either real
database, create a real backup, or run embedding lifecycle maintenance.
