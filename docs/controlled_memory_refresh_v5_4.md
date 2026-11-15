# YunOS v5.4 Controlled Memory Refresh Pipeline

v5.4 connects the already sealed v5.1, v5.2, v5.3, and v5.0.1 capabilities
without introducing a parallel summary or embedding system. It processes one
conversation per invocation, defaults to read-only status, and keeps the two
production writes independently confirmed.

## Architecture

The workflow has six explicit phases:

1. `status` reads current state and recommends one next action.
2. `prepare-summary` regenerates one deterministic candidate and acceptance
   plan without writing.
3. `execute-summary` accepts only the confirmed candidate/acceptance plan into
   the append-only memory sidecar.
4. `prepare-semantic` builds a v5.4 semantic plan for the exact latest accepted
   version without writing.
5. `execute-semantic` re-plans and executes only the independently confirmed
   semantic plan.
6. `verify` checks formal-summary existence, v5.1 coverage, semantic lifecycle
   state, stored semantic identity, retrieval, and read-only source handling.

Only this shared full-verification gate can emit `completed`. `status`, the
no-action path of `prepare-semantic`, the post-write path of
`execute-semantic`, and explicit `verify` call the same gate. No public entry
uses lifecycle `up_to_date` alone as a completion condition.

There is no cross-database transaction. A committed accepted summary followed
by a missing, failed, stale, or busy semantic operation is a durable recoverable
state. A later invocation derives the next action from the two sidecars and
does not accept the summary again.

## Unified summary identity

When a memory sidecar is supplied, semantic planning selects:

1. the latest accepted `summary_versions` row for a conversation;
2. otherwise the latest compatible legacy `memory_summaries` row;
3. otherwise no summary.

Sidecar embeddings use `source_table='summary_versions'`; legacy embeddings
retain `source_table='memory_summaries'`. Existing semantic uniqueness already
includes `source_table + source_id`, so equal IDs from the two stores remain
unambiguous without a schema migration.

A controlled plan binds:

- `summary_source`, `source_table`, `summary_id`, `conversation_id`;
- `summary_version`, `summary_text_hash`, and `memory_space`;
- `candidate_id`, `acceptance_plan_id`, and `source_snapshot_hash`;
- the complete provider identity and `semantic_target_kind`.

The same source-qualified fields are stored in existing embedding
`metadata_json`. A newer summary version, different text hash, changed provider
identity, or changed target produces a different plan ID and makes the old plan
stale. The executor always reloads the exact confirmed source record; it never
substitutes the latest summary after confirmation.

## Independent confirmation envelopes

Summary execution requires `--confirm-candidate-id` and
`--confirm-acceptance-plan-id`, plus the existing production memory-sidecar
flags when the production target is selected.

Semantic execution requires `--confirm-semantic-plan-id`, plus the existing
production semantic-sidecar flags when the production target is selected.

An acceptance plan ID cannot authorize semantic maintenance, and a semantic
plan ID cannot authorize summary acceptance. There is no shared `--yes` flag.
Missing or mismatched confirmation returns `refused` or `stale_plan` before a
backup or write.

## Recovery and state model

The public status vocabulary includes:

- `sidecar_not_initialized`;
- `summary_acceptance_required`;
- `summary_accepted_index_pending`;
- `semantic_plan_ready` and `semantic_plan_stale`;
- `maintenance_busy`, `failed`, and retry actions;
- `completed` and `manual_review`.

Repeated acceptance of the same candidate resolves to the stored accepted
version and does not append a duplicate. Compatible embeddings resolve to
`completed` with no write only after metadata binding and retrieval also pass.
Missing, damaged, or misbound metadata returns
`manual_review/repair_semantic_metadata`; a missing or failed retrieval returns
`manual_review/inspect_semantic_retrieval`; an incompatible lifecycle returns
the semantic prepare/reindex action. Failed or busy semantic execution can be
retried using a freshly validated plan without creating another summary version.
Semantic busy and no-action outcomes create no retained backup; transactional
non-unique failures roll back the entire production semantic batch.

## Audit and privacy

Every workflow response includes a workflow run ID, conversation and plan
identities, accepted version/hash, safe provider summary, phase, status, next
action, commit flags, backup metadata, timestamps, and
`readonly_source=true`.

Default output excludes message text, summary text, embedding vectors,
absolute paths, user directories, exception stacks, and internal prompts.
`--show-candidate-text` is the only candidate-text preview gate; preview text
is returned only in that response and is not persisted by the workflow.

## Safety boundary

The original chat database is opened read-only by the component readers and is
never migrated. The memory and semantic sidecars retain their v5.3/v5.0.1
file-identity, reparse, hard-link, transaction, confirmation, backup, and
concurrency gates. Tests use system-temporary databases and the deterministic
test provider; no network, external API, LLM, or model download is required.

The workflow does not initialize the production memory sidecar, accept a real
candidate, execute a real embedding write, batch the missing-summary backlog,
run a scheduler/daemon/watcher, merge `main`, or create a release tag.

## Verification coverage

The v5.4 test path covers missing summary through candidate, accepted v1,
source-qualified semantic plan, compatible embedding, final dual
`up_to_date`, and semantic retrieval. It also covers complete rerun
idempotency, interruption between databases, failed-index retry, a newer
accepted version invalidating an old semantic plan, independent confirmation
envelopes, legacy fallback, cross-source ID collision, file-identity aliases,
busy and rollback outcomes, text-free/path-free audit output, unchanged source
hashes, and immediate Windows temporary-database deletion.

The final verification-gate regression additionally corrupts or removes
semantic metadata, misbinds version/hash, simulates incompatible provider
identity, suppresses or fails retrieval, and checks a newer accepted version
against an older embedding. Every public completion view must agree, remain
text/path/vector-free, and leave source, sidecars, backups, and logs unchanged.

Production acceptance remains read-only: only status, prepare phases, verify
where applicable, and deliberately incomplete execute calls are permitted.
