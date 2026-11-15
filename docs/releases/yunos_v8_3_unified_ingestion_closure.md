# YunOS v8.3 Unified Ingestion Closure

## 1. Closure scope

v8.3 closes the production-capable Unified Ingestion line. It binds immutable
source evidence to deterministic normalized records, an exact plan, separate
approval, atomic execution, verified asset promotion, replay protection,
recovery, and append-only audit evidence.

The closure does not activate v8302, Historical Reconstruction, historical
backfill, another source, persistent writer authority, or conflict decisions.

## 2. Unified ingestion chain

| Layer | Closed result |
| --- | --- |
| SourceManifest | Exact source type/version, adapter, archive SHA/size, counts and warnings |
| ImportBatch | Full/incremental state machine with plan and approval binding |
| Adapter | Official ChatGPT export discovery and deterministic normalization |
| Normalized Records | Stable source identities, provenance, messages, attachments and entities |
| Planner | Exact append/no-change/conflict operations against a target snapshot |
| Approval | Canonical package digest with execution authorization remaining false |
| Human authorization | Separate one-time record bound to code/package/plan/source/database/assets |
| Atomic Commit | `BEGIN IMMEDIATE`, exact operations, no legacy rewrite |
| Verification | Expected deltas, integrity/FK/schema, data and asset invariants |
| Recovery | Pre-commit rollback and post-commit recovery-required/backup restore |
| Audit | SourceManifest, Batch, Execution, Verification and chained events |

## 3. v8301 production migration

v8301 was applied through the committed exact production entry:

- version: 8301;
- digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- target schema:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- result: completed;
- rollback: not triggered;
- recovery-required: false;
- v8302: absent.

The measured service quiet window was 688.693 seconds. It exceeded the original
600-second estimate by 88.693 seconds without violating database, service, or
authorization invariants.

## 4. Official source and exact plan

- source kind: official ChatGPT export ZIP;
- archive size: 839,802,142 bytes;
- archive SHA-256:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- SourceManifest digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- ImportPlan digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- one-time plan:
  `realplan_8e3b5690e4481627f09d9821497f8b3b980fb2d5707b6652`.

The normalization result contained 408 conversations and 15,799 stable
messages. Planning identified 15,636 exact production duplicates, 163 new
old-time/new-ID messages, no message conflicts, and one conversation-field
conflict.

## 5. Approval and atomic execution

The approval package retained:

`production_execution_authorized=false`

Execution authority came only from the separate one-time
HumanAuthorizationRecord bound to the final code, package, ImportPlan,
SourceManifest, archive, database, imports, backup, and absent pre-import asset
baseline.

The exact execution:

- appended 163 messages;
- did not modify any existing conversation or message;
- created 874 AssetEntities;
- created 1,538 attachment bindings;
- recorded 13 pending conflicts;
- committed the database transaction before asset promotion;
- promoted only the verified 874-entity set;
- completed database-and-asset verification;
- consumed the one-time plan;
- released the writer lease.

## 6. Asset inventory, staging and promotion

The shared `v8.3-asset-baseline-1` contract distinguishes absent, empty, and
populated roots. It canonicalizes Unicode NFC/POSIX relative paths, hashes
every file, sorts deterministically, preserves case while rejecting
case-fold collisions, and rejects symlink/reparse/junction/UNC/device paths.

Final verified promotion:

- unique AssetEntities: 874;
- logical bindings: 1,538;
- physical files: 874;
- physical bytes: 540,711,854;
- inventory digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- AssetBaseline digest:
  `0000000000000000000000000000000000000000000000000000000000000000`.

Excluded from promotion:

- 12 ambiguous logical assets;
- 35 missing assets;
- 7 unsupported/external assets;
- 412 size/MIME-invalid assets;
- 1,122 metadata-only attachments.

## 7. Pending conflicts

The final conflict ledger contains:

- 12 `asset_locator_ambiguous`;
- 1 `conversation_fields_changed`;
- 13 pending;
- 0 decisions;
- 0 resolved.

No automatic or default decision occurred. No ambiguous entity entered the
promotion set. The pending records are explicit evidence, not silent loss.
They can be handled only through a future separately authorized human process.

## 8. Replay and rescan

- clone replay: `already_applied`;
- production formal-entry replay: `one_time_execution_replayed`;
- archive rescan normalized messages: 15,799;
- rescan exact duplicates: 15,799;
- rescan new messages: 0;
- rescan message conflicts: 0.

Replay does not decide conflicts, append duplicates, promote another asset, or
create a second execution.

## 9. Production invariants

Final production state:

- conversations: 426;
- messages: 17,003;
- legacy assets: 172;
- AssetEntities: 874;
- bindings: 1,538;
- pending/resolved conflicts: 13 / 0;
- integrity/FK/WAL: `ok` / 0 / 0;
- recovery-required: absent;
- writer lease: absent;
- v8301: present;
- v8302: absent.

The pre-existing 15,636 duplicate messages were not rewritten. Existing
conversation, message, source/source_id, attachment JSON, and legacy-asset
digests remained equal. Profile, Semantic, Runtime, and imports content gates
remained unchanged.

## 10. Recovery

Clone rehearsal covered pre-commit rollback, post-commit failure,
recovery-required refusal, four-database restore, and asset-root restore.
Production rollback was not triggered.

The final release recovery package is:

`backups/releases/yunos-v8-final-20260730T152202526616Z/`

Its backup-set digest is:

`0000000000000000000000000000000000000000000000000000000000000000`

An independent temporary restore validated four databases, the full asset
tree, all verified binding targets, old and new message search, conversation
reading, isolated real-readonly startup, GET-only/no-store behavior, complete
POST 403, no writer lease, and zero WAL. The temporary instance was removed.

## 11. Read-only product posture

Production continues with:

- one loopback listener on port 8765;
- `service_mode=real-readonly`;
- `readonly=true`;
- `can_execute=false`;
- Tailnet-only private HTTPS;
- Funnel disabled;
- no persistent writer or writer lease.

Desktop, mobile/PWA, and API verification are read-only. Private API responses
remain `no-store`, application CacheStorage excludes `/api/`, and product POST
requests receive complete HTTP 403 responses.

## 12. Deferred and excluded work

The following are retained but not production-activated:

- G0 and v8302;
- Historical Reconstruction and historical backfill;
- legacy provenance repair without trustworthy sources;
- another source import;
- automatic conflict resolution;
- mobile writes, public exposure, or v8.4.

This closure ends at the final release approval gate. Local `main`, the
proposed annotated tag, and all remote refs remain unchanged until separately
authorized.
