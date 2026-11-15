# YunOS v6.2 Controlled Knowledge Persistence Gateway

## Scope and non-goals

v6.2 consumes only a current, authoritative v6.1 `accept` decision whose stored
status is `accepted_pending_persistence`. It writes one version-1 formal
`semantic_related` relation to knowledge schema version 3. It does not infer new
facts, accept candidates, initialize production state, migrate a production
database, contact an external service, or modify chat, memory, or semantic
storage.

Production remains sealed. The production CLI exposes read-only `status` and a
`persist` command that always returns `production_enablement_incomplete` in this
release. It accepts no database, backup, authority, session, anchor, init, or
restore path. Development execution is possible only through the internal API
with `test_mode=True`, an already initialized knowledge database, and an
already existing backup directory, all under the OS temporary root.
The read-only preview emits a deterministic `persistence_id`; execution refuses
unless the caller explicitly returns that exact current ID.

Discovery itself is not a caller input. A sealed production provider rebuilds
from the fixed read-only chat database, memory sidecar, and semantic sidecar.
A private test provider reads a digest-checked graph source restricted to the
OS temporary root. Replacing that source invalidates an earlier preview.

## Current-fact revalidation

A persistence request contains only `candidate_id`, `review_plan_id`, and
`decision_id`. The gateway does not accept a client candidate, plan, decision,
supersession, graph node, evidence record, authority identity, or persisted
flag. A process-issued Review Authority handle reloads the registry and ledger
and performs the complete v6.1 validation protocol before returning the stored
objects and mutation head.

The gateway then proves:

1. candidate, plan, and decision IDs form one authority-owned chain;
2. every stored identity and payload hash is valid and cross-bound;
3. the decision is exactly `accept`, remains
   `accepted_pending_persistence`, and is not marked persisted;
4. there is no supersession for the candidate/decision and no recorded
   successor for the chain;
5. a freshly trusted discovery contains the same immutable candidate;
6. plan verification reports the same recorded accept decision;
7. both graph nodes exist in the current discovery, their payload hashes match,
   and the initialized knowledge sidecar stores the same field-level node
   identities;
8. SQLite integrity, foreign keys, user version, required tables, triggers,
   indexes, constraints, and the canonical schema fingerprint match.

Preview performs a complete rebuild and validation. Execution then acquires a
per-store OS coordination lock plus an Authority reservation in the same
registry-then-ledger order used by Authority mutations. Both Authority locks
remain held through backup, a second fixed-source rebuild, and final knowledge
commit. Old graph builds,
rejected/deferred/replaced decisions, superseded chains, altered schemas,
missing nodes, identity collisions, and mismatched request IDs fail closed.

## Transaction and idempotency

Every independently initialized knowledge sidecar has a random immutable store
ID, generation, initialization seal, canonical schema fingerprint, and metadata
digest. The gateway creates a deterministic `persistence_id` from the protocol,
candidate, plan, decision, exact Authority mutation head, complete knowledge
store binding, and anchor assessment. A confirmation from one store cannot
authorize another store. The
database enforces unique candidate, plan, decision, and review-chain bindings.
It also permits only one active formal relation for the normalized
relation/source/target tuple, so a different accepted candidate cannot create a
duplicate official edge.
One `BEGIN IMMEDIATE` transaction appends:

- `accepted_relation_versions`: the formal relation version, current endpoint
  payload hashes, evidence and graph-build identities, authority provenance,
  acceptance time, and active status;
- `persistence_receipts`: deterministic transaction/receipt identities,
  complete backup and knowledge-store identities, exact Authority mutation
  sequence/digest, anchor enabled/satisfied flags, anchor identity, assertion
  and reason digests, and
  the explicit `production_eligible=0` result.

An exact retry returns the existing receipt and does not add a relation or
receipt. A different identity collision is refused. Tables are append-only via
update/delete triggers.

## Backup, interruption, and recovery

Before the write transaction, SQLite's backup API creates a consistent copy in
the authorized temporary backup directory. The manifest binds its schema,
source sidecar path, persistence ID, backup path, SHA-256, backup identity,
creation time, and non-production status under a canonical manifest digest. It
is created with exclusive permissions and flushed to disk.

Failure after backup or before commit leaves no formal relation. Failure after
commit leaves the complete relation and receipt, so retry converges
idempotently. Restore takes the same per-store coordination lock as persistence,
verifies manifest digest, target/store bindings, path policy, backup checksum,
and current target hash, then emits a deterministic restore ID. Execution
requires that exact confirmation, revalidates the restored schema/store, and
writes a recovery receipt beside the manifest. All SQLite connections and lock
handles close in `finally` paths.

The local manifest is not an external rollback anchor. It provides operational
backup/recovery only within the same trust boundary.

## External anchor interface and production enablement

`ExternalStateAnchor.assess` is the reserved interface. It receives only the
current authority store ID and mutation sequence/digest and returns whether an
independently durable anchor is enabled and satisfied. A configured but
unsatisfied assessment refuses persistence. The built-in implementation is
disabled, makes no network call, and reports unsatisfied. Temporary tests may
exercise the contract but every receipt remains `production_eligible=0`.

Production must not be enabled until all of these are independently delivered
and reviewed:

- approved production initialization/migration with a fixed schema identity;
- explicit, auditable operator write authorization and least-privilege keying;
- validated production backup retention, restore authorization, and disaster
  recovery drills;
- a durable external authority anchor outside the local rollback boundary;
- proof that the anchor matches the current authority mutation head at commit;
- concurrency/locking, tamper, rollback, crash, privacy, and security review;
- an independently approved CLI/API enablement change.

## Verification

The focused suite covers exact retry, exception and real subprocess hard-exit
interruptions, backup restore, schema tampering, stale fixed-source discovery,
cross-store confirmation substitution, concurrent Authority mutation,
persist/restore lock exclusion, complete anchor receipt fields, production path
refusal, and sealed CLI behavior. v6.0 graph/knowledge and v6.1 authority/CLI
suites remain regression dependencies.
