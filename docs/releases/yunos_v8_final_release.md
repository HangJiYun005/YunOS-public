# YunOS v8 Final Release

## 1. Release decision boundary

YunOS v8 delivers a private, evidence-backed local memory product from trusted
Runtime execution through desktop, private mobile access, and unified
ingestion. Its authoritative scope contains exactly v8.0 through v8.3.

This document prepares the final release candidate. It does not authorize or
perform the local `main` fast-forward, annotated tag creation, remote push,
another production migration, another ImportPlan, conflict resolution, v8302,
Historical Reconstruction, or v8.4.

## 2. Four completed stages

| Stage | Delivery | Final state |
| --- | --- | --- |
| v8.0 | Trusted Runtime, approved profile, reviewed binding, at-most-once DeepSeek execution, append-only Runtime evidence | Closed |
| v8.1 | Production desktop shell, Omnibox, search, document-style results, contextual sidebar, conversation reading | Closed |
| v8.2 | Tailnet-only mobile/PWA access, GET-only APIs, private text cache, offline reading/search and state restoration | Closed |
| v8.3 | SourceManifest through recovery, v8301 production migration, verified official ChatGPT import and asset preservation | Ready for final release approval |

## 3. v8.0 Trusted Runtime

v8.0 established fixed-path production delivery, an approved-profile control
plane, deterministic request artifacts, explicit reviewed execution binding,
at-most-once provider send, append-only Runtime events and receipts, fail-closed
recovery, authority-free review, and isolated runtime administration.

The first controlled production invocation completed with intact execution and
audit guarantees. Its bounded-output truncation remains an answer-quality
limitation rather than an execution-integrity failure.

## 4. v8.1 Desktop Product

v8.1 delivered the production desktop shell and read-only workflow:

- minimal home and Omnibox;
- memory search and document-style results;
- contextual sidebar;
- conversation details and message context;
- explicit production/readonly state;
- no hidden write authority in normal product mode.

## 5. v8.2 Private Mobile Access

v8.2 delivered a loopback-only host behind Tailnet-only private HTTPS:

- mobile home, search, five-message preview and conversation reader;
- message anchors, return-state restoration and recent records;
- IndexedDB private text cache with TTL, LRU and bounded retry;
- cached-text offline reading and cached-only offline search;
- 24-hour UI state restoration and responsive viewport behavior;
- application-shell CacheStorage separated from private API content;
- no SQLite copy, mobile write, AI/Runtime cache, Funnel, or public listener.

Earlier iPhone Safari/PWA acceptance remains cumulative evidence. The absence
of a current Mac+iPhone Web Inspector environment remains an explicit,
non-blocking limitation and is not represented as completed evidence.

## 6. v8.3 Unified Ingestion

v8.3 provides one controlled pipeline:

`Source -> SourceManifest -> ImportBatch -> Adapter -> Normalized Records -> Planner -> Approval -> Atomic Commit -> Verification -> Recovery/Audit`

It includes deterministic replay, conflict refusal, writer leases,
recovery-required handling, verified rollback, asset inventory/staging/
promotion, and a shared AssetBaseline contract.

The release retains G0, v8302, Historical Reconstruction, and backfill code as
`preserved_out_of_scope_engineering`. They are not production prerequisites
and were not activated.

## 7. v8301 production migration

The exact v8301 migration completed under a one-time HumanAuthorizationRecord.

- migration digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- schema changed additively from the legacy baseline to
  `0000000000000000000000000000000000000000000000000000000000000000`;
- legacy conversations/messages/assets remained 426 / 16,840 / 172;
- v8302 was not applied;
- rollback was not triggered and `recovery_required=false`.

The measured service quiet window was 688.693 seconds, 88.693 seconds above
the original 600-second estimate. This operational variance is retained
explicitly and did not change the migration or data-invariant result.

## 8. Verified official ChatGPT import

The production import used one official ChatGPT export:

- archive size: 839,802,142 bytes;
- archive SHA-256:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- SourceManifest digest:
  `0000000000000000000000000000000000000000000000000000000000000000`;
- ImportPlan digest:
  `0000000000000000000000000000000000000000000000000000000000000000`.

The exact result was:

- 408 source conversations and 15,799 normalized stable messages;
- 15,636 production exact duplicates not rewritten;
- 163 appended messages, all old-time/new-ID;
- archive rescan proposed 0 additional messages and 0 message conflicts;
- replay returned the already-applied/one-time-consumed refusal.

## 9. Real asset preservation

The import created and promoted exactly:

- 874 verified AssetEntities;
- 1,538 attachment bindings;
- 874 physical files;
- 540,711,854 physical bytes.

The final AssetBaseline is:

- schema `v8.3-asset-baseline-1`;
- state `populated`;
- inventory digest
  `0000000000000000000000000000000000000000000000000000000000000000`;
- baseline digest
  `0000000000000000000000000000000000000000000000000000000000000000`.

Ambiguous, missing, external/unsupported, invalid, and metadata-only items were
not silently promoted. The release does not claim that all 1,368 archive asset
entries were uniquely preserved.

## 10. Controlled pending conflicts

Production contains 13 controlled pending conflicts:

- 12 `asset_locator_ambiguous`;
- 1 `conversation_fields_changed`;
- 0 decisions and 0 resolved conflicts.

No agent or default action decided them. The ambiguous set did not enter asset
promotion. They remain available for a later separately authorized human
workflow and do not affect current read-only search or conversation reading.

## 11. Legacy provenance limitation

Some legacy rows predate trustworthy source evidence and cannot be
retroactively bound without fabrication. They remain classified as
`legacy_provenance_limitation`.

The release preserves that limitation instead of rewriting legacy source IDs,
attachment JSON, or provenance. Historical Reconstruction and v8302 remain
deferred and were not used to make v8 appear complete.

## 12. Final production baseline

| Item | Final value |
| --- | --- |
| Main size | 76,333,056 bytes |
| Main SHA-256 | `0000000000000000000000000000000000000000000000000000000000000000` |
| Main schema | `0000000000000000000000000000000000000000000000000000000000000000` |
| conversations / messages / legacy assets | 426 / 17,003 / 172 |
| AssetEntities / bindings | 874 / 1,538 |
| pending / resolved conflicts | 13 / 0 |
| integrity / FK / WAL | `ok` / 0 / 0 bytes |
| migration | v8301 present; v8302 absent |
| recovery / writer lease | absent / absent |

Profile, Semantic, and Runtime retain their authenticated size, SHA, schema,
logical digest, integrity, and zero-FK baselines. Production remains
`real-readonly`, `readonly=true`, and `can_execute=false`.

## 13. Final recovery package

The final recovery package is:

`backups/releases/yunos-v8-final-20260730T152202526616Z/`

It contains four SQLite online backups, a complete 874-file asset snapshot,
per-file asset hashes, source/backup database fingerprints, and sanitized
release metadata. The official 839 MB archive is referenced by exact size and
SHA rather than duplicated.

Backup-set digest:

`0000000000000000000000000000000000000000000000000000000000000000`

The independent restore rehearsal passed database integrity/FK/schema/marker/
count checks, the exact asset baseline, binding-to-file validation, old and new
message search, conversation readers, isolated real-readonly startup, GET 200,
`no-store`, complete POST 403, zero writer lease, and zero WAL. The temporary
restore instance was removed.

## 14. Final validation

The authoritative final repository-wide regression ran once after the final
candidate documentation was prepared:

- command: `python -B -m unittest discover -s tests -q`;
- result: 1,395 tests, 0 failures, 0 errors, 5 existing skips;
- wall time: 161.570 seconds;
- test-runner reported time: 160.379 seconds;
- log SHA-256:
  `0000000000000000000000000000000000000000000000000000000000000000`.

The release also requires and records Python compileall, JavaScript syntax and
browser checks, PowerShell AST, `git diff --check`, documentation links,
changed-file privacy scanning, release-manifest digest validation, and
production GET-only desktop/mobile/API checks.

## 15. Known limitations and security boundaries

- legacy provenance gaps remain explicit;
- 13 conflicts remain pending and unrepresented as resolved;
- only 874 uniquely verified assets are claimed;
- current Web Inspector evidence is cumulative rather than newly generated;
- local backup files require the same protection as production data;
- production remains read-only with no persistent writer;
- local application guarantees do not defend against an administrator or
  process with direct filesystem control;
- provider/network correctness remains outside the local trust boundary.

## 16. Excluded future directions

The final v8 release excludes:

- v8302 and Historical Reconstruction production execution;
- historical backfill;
- another source import;
- media or AI/Runtime private caching expansion;
- mobile writes or database synchronization;
- public exposure or Funnel;
- v8.4 or any new schema generation.

## 17. Final completion criteria

| Criterion | Result |
| --- | --- |
| v8.0 Runtime closed | Pass |
| v8.1 Desktop closed | Pass |
| v8.2 Mobile/PWA closed | Pass |
| v8301 migrated safely | Pass |
| Official real-source import closed loop | Pass |
| Replay/conflict/failure/recovery gates | Pass |
| No fabricated provenance or assets | Pass |
| Desktop/mobile/legacy read compatibility | Pass |
| Final recovery package and rehearsal | Pass |
| Final regression | Pass: 1,395 tests; 0 failures/errors; 5 existing skips |
| local `main` fast-forward | Not authorized in preparation |
| annotated final tag | Not authorized in preparation |
| main-control GO | Awaiting final user approval |

The prepared tag name is `v8.3-unified-ingestion`. The prepared target is the
final candidate commit created after this document. No tag or `main` update is
performed by this preparation.
