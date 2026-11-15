# YunOS v6.1 Relation Candidate Discovery and Review Authority

YunOS v6.1 is a read-only candidate-review protocol over trusted v6.0 graph
builds. It never creates a production knowledge edge. `persisted=false` always
means that no candidate reached the production graph.

## Candidate and plan identity

Candidate identity binds schema, relation, normalized endpoints and node
payload hashes, direction, generator/configuration, evidence digest and kind,
score, threshold, status, and graph-build identity. Time, query order, row
numbers, paths, text, vectors, and raw metadata are excluded. Only discovered
`semantic_related` candidates can enter human review.

A plan additionally binds the fixed review policy and allowed decisions plus:

- `authority_store_id`, schema version, generation, metadata digest, and
  protocol identity;
- a factory-issued `review_session_id` and unique `review_chain_id`;
- the candidate, both nodes, evidence, generator/configuration, score,
  threshold, relation/direction, and graph build.

`generated_at` is not part of plan identity. Prepare, verify, decide, and
replacement require a process-issued `ReviewAuthorityHandle`; its public
constructor is disabled and callers cannot pass a store, path, authority ID,
session token, or importable sentinel. A forged or cross-authority handle fails
closed. Test authority creation is private test support and is not imported or
selectable by the production CLI.

## Review Authority

The production protocol reserves these fixed files:

- `data/review/ayunos_review_authority_registry.db`
- `data/review/ayunos_review_authority.db`

v6.1 deliberately provides no production initializer. `list` works while the
authority is absent; stateful commands return
`review_authority_not_initialized`, `executable=false`, and `persisted=false`
without creating `data/review`.

Tests may explicitly initialize a complete replica only below the OS temporary
root. Registry and ledger identities bind a random store ID, generation,
ledger instance, initialization seal, schema fingerprints, metadata digest,
and protocol. Every mutation locks registry then ledger, appends ledger state,
advances the ledger mutation head, and then advances the registry head.
Missing files, empty substitutions, identity mismatches, schema changes, or
unequal mutation heads are refused without repair, migration, or re-creation.
Every open also runs SQLite integrity and foreign-key checks, reconstructs and
validates every stored protocol object and cross-record reference, and proves
that every plan and decision is covered exactly once by the continuous
mutation-event digest chain. Appending rows without advancing that chain fails
closed.

Both SQLite files are checked against canonical `sqlite_master` fingerprints,
covering tables, columns, keys, foreign keys, unique constraints, indexes, and
append-only update/delete triggers. Unknown schemas, relations, statuses,
decisions, reason codes, non-finite numbers, malformed hashes/IDs, expanded
decision lists, and inconsistent cross-record references are rejected even on
the first append.

## Unique review chains and decisions

Within one authority, repeated prepare of the same candidate and policy returns
the same chain and plan. A new session cannot create a parallel chain. A later
graph build for the same relation/direction/endpoints creates one successor
binding to every old active chain in that family, so all old-build plans become
stale atomically. Alternative candidates in the same immutable build may
coexist so one can be a real replacement target.

A plan has at most one decision. Exact retries are idempotent and preserve the
decision ID; a different decision returns `decision_conflict/manual_review`.
Accept is only `accepted_pending_persistence`; reject does not delete; defer
does not imply acceptance. Reviewer identity is emitted only as a stable
namespaced digest.

## Replacement and supersession

Replacement requires another currently discovered candidate and its own active
plan in the same authority. It preserves relation type, direction, and
normalized endpoints. The confirmation, decision, and supersession bind both
candidates, both plans, payload hashes, chains, authority, and replacement
policy. A committed replace atomically records the decision and append-only
supersession; the old plan never becomes active again.

`replacement-preview` is non-mutating and reports `current_status=candidate`,
`proposed_effect=would_supersede`, `coordination_recorded=false`, and
`persisted=false`. It never claims that preview already superseded anything.

## CLI and privacy

The default command remains bounded read-only listing:

```powershell
python tools/review_memory_graph_candidates.py list --limit 5 --json
python tools/review_memory_graph_candidates.py prepare --candidate-id <id> --json
python tools/review_memory_graph_candidates.py verify --plan-file <plan.json> --json
python tools/review_memory_graph_candidates.py decide --plan-file <plan.json> --confirmation-file <confirmation.json> --decision accept --reason-code evidence_reviewed --reviewer-identity <identity> --json
```

There is no ledger-path, authority-ID, new-session, init, write, execute,
accept-all, or arbitrary-candidate option. Output excludes authority file paths,
user directories, sensitive session material, message/summary text, embeddings, raw
metadata, prompts, raw reviewer identity, and exception stacks.

## Threat model and v6.2 handoff

The registry detects loss, replacement, or rollback of the ledger while the
current registry survives. A new full replica has a different authority
identity, so old plans fail. Detecting malicious rollback or destruction of the
entire registry-and-ledger root requires an external durable anchor; v6.1 does
not claim protection against that threat.

Production initialization, backup/restore authorization, an external anchor,
and knowledge-edge persistence remain explicit v6.2 work. v6.2 may consume
this protocol only after it revalidates the authority, current candidate,
plan, decision, and supersession within its controlled persistence transaction.
