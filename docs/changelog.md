# Changelog

## v8.0-H - Review-only Runtime Preview

- Added the explicit `runtime-review` launch mode over the fixed production
  source and immutable readonly Runtime status inspection.
- Added a pure preview facade that uses the released Delivery, D1 artifact, and
  v8.0-G Reviewed Binding builders while importing no production execution
  facade, authority issuer, v4 provider Adapter, credential loader, HTTP, or
  network surface.
- Added exact caller-specified request/mutation ID fields, full reviewed object
  rendering, uncertainty and final-context panels, and clearly estimated input
  token reporting.
- Added visible unchecked/disabled confirmation and disabled Send controls with
  an authority-absent explanation.
- Added an HTTP 403 server gate before request-body parsing for every
  review-only execute POST, plus focused startup/import, crafted-request,
  protocol-stability, fixed-ID, and UI tests.
- Preserved the released v8.0-G Reviewed Binding protocol and the separate
  explicit `runtime-admin`, `runtime-admin-mock`, and `web-real-readonly` modes.

## v8.0-G - Production Runtime Execution UI Closure

- Added the fixed D1 production execution service over the shared v8.0-E
  Delivery, immutable D1 artifact, D1 sidecar, DeepSeek v4 Flash profile, and
  canonical D1 ledger identity/receipt validators.
- Added full preview diagnostics for Delivery, Assembly, D1 artifact,
  model-context hash, wire-body hash, profile projection, request fingerprint,
  model run, conversation, request ID, and mutation ID.
- Split process-local Delivery/Assembly/D1 artifact diagnostics from the
  versioned reviewed execution authority. The reviewed binding seals exact
  question, budget, ordered profile/evidence anchors, model context, fixed
  provider configuration, wire body, request/mutation IDs, and idempotency
  fingerprint without changing released v7.4 or v8.0-E identity semantics.
- Delayed the private fixed DeepSeek adapter factory until confirmation,
  canonical rebuild, reviewed-binding comparison, read-only Runtime validation,
  and idempotency checks all pass. Rejected preflight constructs no adapter and
  reaches no credential, HTTP request, or network surface.
- Replaced the production Web runtime route's legacy B/C executor with the D1
  service and removed production recovery/fallback routing. The B/C module and
  CLI remain available only for their versioned legacy protocol and fixtures.
- Added the Runtime Execution page with complete reviewed context, fixed
  configuration, profile/evidence anchors, explicit confirmation, response,
  actual usage, execution phase, and exactly-one-call/no-retry evidence.
- Added a temporary-sidecar, mock-transport visible acceptance mode. It refuses
  the production Runtime sidecar and performs no credential or network I/O.
- Replaced the D1 adapter's environment/legacy-loader fallback with an exact
  fixed `config_private/deepseek.env` loader. Credential values remain outside
  artifacts, ledgers, responses, logs, and UI state.

## v8.0-D2.2 - Deterministic SQLite Snapshot Encoding

- Added a versioned ASCII-safe canonical encoder for all five SQLite storage
  classes. TEXT preserves strict UTF-8 bytes, hashes, and code points; BLOB
  preserves arbitrary bytes through base64, hash, and length without coercion.
- Added deterministic row, table, schema, and database identities with strict
  introspected identifiers and explicit primary-key/rowid ordering.
- Added a fixed read-only production profile snapshot entrypoint and additive
  pre/post row verification. It accepts no production path, capability, policy,
  credential, or write dependency and changes no D2 authorization protocol.
- Updated the R2 runbook so BLOB-bearing D2 manifest/result/receipt rows are
  snapshotted before any profile-admin or capability boundary.

## v8.0-B/C - DeepSeek Runtime Thin Slice

- Added a deterministic request artifact binding the live source-backed v7.4
  delivery, final model context, v8.0-A approved-profile authority projection,
  system instruction version, fixed DeepSeek configuration, and payload hash.
- Added a transport-only single-provider, single-turn, non-streaming DeepSeek
  adapter. Credentials remain environment/ignored-local-config only and never
  enter artifacts, databases, logs, API results, frontend state, or Git.
- Added an independently initialized append-only runtime sidecar for
  conversations, messages, request artifacts, model runs, state events,
  sanitized errors, and immutable receipts. No chat, semantic, memory,
  knowledge, profile, or review database is written.
- Added the event-derived `prepared -> running -> succeeded|failed` state
  machine, exact request/mutation idempotency, same-key concurrency exclusion,
  terminal receipt replay, and explicit no-resend recovery for interrupted
  running calls.
- Added fixed-source preview/status and explicit runtime-admin execute/recovery
  CLI/API surfaces. Production profile/runtime sidecars remain uninitialized by
  default; all tests use temporary sidecars and mock providers with no network.

## v8.0-A Lite - Approved Profile Control

- Added a new control layer over sealed v7.2 candidates without changing any
  v7.0-v7.4 protocol, validator, identity, or preview contract.
- Added candidate and source-evidence review plus explicit `approve`,
  `edit_and_approve`, `reject`, and `defer` decisions. Open questions remain
  non-promotable.
- Added an independently initialized append-only decision sidecar. SQLite
  triggers prohibit decision/receipt update and delete, while the active approved
  profile is derived from the latest decision per candidate. Decisions are
  fixed to `decision_source=manual`, complete history is reviewable, and active
  items explicitly carry `model_visible=true`.
- Added global `expected_revision` optimistic concurrency and exact retry
  idempotency through unique `request_id` and `mutation_id` values plus immutable
  first-result receipts.
- Added fixed real-readonly preview and a separate explicit local profile-admin
  mode. Ordinary readonly rejects every POST; profile-admin rebuilds the
  source-backed v7.2 package at execution time and writes only the profile
  sidecar while the source chat database remains read-only.
- Added an explicit dry-run-first initializer with separate production allow and
  confirmation flags and strict boolean validation. No production profile,
  memory, knowledge, review, or
  runtime sidecar is initialized by default.
- Added a complete temporary-sidecar test loop and audit/privacy boundaries.
  Runtime, DeepSeek API, model calls, chat UI refactoring, automatic approval,
  shadow approval, and full dormant/expired lifecycle remain out of scope.

## v7.4 - Transparent Context Review and Delivery

- Added protocol `v7.4-transparent-context-delivery-1` and deterministic `dvy_`
  identities that bind the complete original v7.3 assembly, normalized controls,
  temporary exclusions, final layers, and exact final model context.
- Added a self-reconstructing delivery validator over the existing v7.3
  standalone/source-backed production chain without adding retrieval, ranking,
  candidate promotion, or persistence logic.
- Added per-delivery whole-item exclusions for approved profile, unapproved
  candidates, and historical evidence. Exclusions never modify source profile,
  candidate, history, selection, or SQLite state.
- Added typed source/evidence/event provenance to candidates, historical
  evidence, relations, and expanded excerpts. Exclusions now resolve a
  deterministic provenance closure; validators recompute it and prove excluded
  provenance is disjoint from the final model payload without deleting equal
  text from independent sources.
- Added explicit approved-profile empty state, `candidate/not_approved` and
  historical-evidence trust labels, source anchors, omission reasons, character
  and token estimates, and capability degradation review.
- Added fixed real-readonly CLI/GET surfaces with no database path, provider,
  profile injection, sidecar, external API/LLM call, or send operation.
- Added the “上下文预览” Web page with query/time controls,
  Small/Medium/Large/Custom budgets, four collapsible layers, include/exclude,
  source-message navigation, identity display, and final model-context preview.
- Hardened source-message navigation to require the composite
  `(conversation_id, message_id)` identity in Web and SQL. Missing, mismatched,
  absent, and ambiguous anchors fail closed without a message-ID fallback.
- Every delivery explicitly reports `persisted=false` and
  `sent_to_model=false`; the preview never claims a model was called.

## v7.3 - Context Assembly

- Advanced the v7.3-only protocol, selection policy, and assembly identity to
  revision 2. Empty-but-degraded v7.1 results now retain capabilities,
  degradation, missing capabilities, and warnings and remain partial.
- Replaced substring relevance with exact normalized Latin tokens and
  conservative Han-segment matching, preventing `Pro` from admitting
  `project` while retaining legitimate MacBook Pro evidence.
- Removed the v7.3 path regex and reused the sealed v7.0 clipping/rejection
  boundary for Windows, UNC, POSIX (`/opt`, `/mnt`), and `file:` URI values on
  every structured and rendered output surface.
- Added protocol `v7.3-context-assembly-2`, selection policy
  `v7.3-context-selection-policy-2`, stable `csp_` policy identities, and stable
  `ctx_` assembly identities without changing v7.0/v7.1/v7.2 contracts.
- Added current-task, approved-profile, explicitly unapproved candidate, and
  citation/anchor-first historical-evidence layers in fixed priority order.
- Added deterministic task-topic filtering so MacBook context excludes unrelated
  game, novel, and family memory while retaining relevant purchase evolution.
- Added Small/Medium/Large/Custom hard budgets for characters, estimated tokens,
  evidence, candidates, and complete history expansions, with body-free
  omission reasons and no semantic truncation.
- Added JSON/text transparency showing exactly what the model saw, exclusions,
  reasons, and character/token estimates.
- Added standalone and source-backed reconstruction, fixed-production readonly
  CLI/GET previews, candidate-to-fact promotion rejection, and production
  zero-write tests.

## v7.2 - Temporal Interpretation and Memory Candidates

- Hardened the protocol to `v7.2-temporal-memory-candidates-4` with one
  expression-level scope scanner that propagates quotes, blockquotes, reports,
  examples, hypotheticals, jokes, and code across candidate segments.
- Rejected empty and reserved all-zero/all-`f` timezone predecessor identities
  in standalone validation while retaining trusted predecessor reconstruction.
- Separated primary-target polarity from excluded targets so an exclusive Pro
  goal remains positive while Air is represented only as excluded.
- Preserved the complete validated v7.1 request/capability/degradation projection
  and prevented partial or degraded sources from being promoted to complete.
- Replaced semantic prefix truncation with an identity-bound safe placeholder,
  added clause hashes/lengths, and strengthened Unicode/fullwidth determinism.
- Advanced timezone config to revision-chain schema 2 with country/region,
  complete predecessor content, and independently trusted predecessor inputs for
  trusted-selection/source-backed validation after first use.
- Closed production source/provider injection while keeping a private synthetic
  test seam and a minimal selected-evidence-only future provider envelope.
- Added deterministic local interpretation for seven candidate types while
  keeping every output candidate-only, readonly, non-persisted, and unapproved.
- Added compact event-ledger history and closed temporal relationship precedence
  for evolution, coexistence, conditional, supersedes, conflict, and unknown.
- Restricted supersedes to explicit exclusive replacement; ordinary preferences
  cannot replace older candidates, and unknown never expires old evidence.
- Added strict statement/reason/event budgets, speculative and overflow
  `needs_review` behavior, evidence anchors, confidence, explicitness, and
  reproducible event/candidate/relation/package identities.
- Added versioned IANA timezone identity with Asia/Shanghai first-use default,
  canonical UTC internal time, predecessor-bound revisions, and no config write.
- Added a disabled-by-default provider interface, fixed-path CLI, production
  real-readonly GET, standalone/trusted-selection/source-backed validators,
  tests, smoke coverage, and audit checks.

## v7.1 - Evidence Selection and Budgeting

- Corrected the review trust contract to match v7.0: standalone validation
  proves internal consistency but may accept a complete coordinated unkeyed
  repackage; source-backed validation is the authenticity boundary. Production
  CLI/API rendering remains gated behind the source-backed production builder.
- Review hardening advances the untagged schema to
  `v7.1-evidence-selection-2` / `v7.1-selection-policy-2`, adds a canonical
  body-free candidate ledger and v7.0 source-state projection, blocks all
  graph fill whenever any direct representative is not admitted, and reuses
  the v7.0 absolute-path boundary during standalone validation and rendering.
- Added the original separate `v7.1-evidence-selection-1` protocol over immutable
  `v7-memory-package-1` inputs.
- Added exact anchor hit merging and deterministic NFC/newline/role duplicate
  equivalence while retaining all source and hit/graph provenance.
- Added Small, Medium, Large, and controlled Custom budgets with an absolute
  model-payload character ceiling and the explicit non-model
  `unicode-codepoint-ceil-div4-v1` estimator.
- Added direct-before-graph selection, deterministic conversation round-robin,
  per-conversation caps, body-free omission manifests, and complete omission
  reason accounting.
- Added standalone derivation/identity validation, v7.0 source-backed rebuild
  validation, fixed-path CLI, and production-readonly GET previews.

## v7.0 - Memory Package Contract

- Sealed graph reason precedence in the shared producer/validator state machine:
  `direct_channel_unavailable` precedes `no_direct_seed`, and knowledge-sidecar
  reasons are reachable only after a legal direct seed exists.
- Closed the final two graph direct-seed blockers: graph-only requests now fail
  shared request normalization with `graph_requires_direct_channel`, and shared
  producer/validator logic binds every graph item to the exact in-envelope
  direct conversation/message, complete canonical hit-type set, and path start.
  Source-backed validation additionally requires that seed to be a direct
  anchor already verified against the trusted read-only database.
- Closed the final graph-envelope review blocker by deriving direct-seed
  capability/proof in shared v6.4 code. Graph evidence now binds canonical
  keyword/semantic `seed_hit_types`; graph-only, unreachable graph-complete,
  and `no_direct_seed` plus evidence combinations fail closed in both validator
  levels after identity recomputation.
- Sealed the follow-up review boundary with a shared v6.4/v7 retrieval envelope
  state machine, derived page-count/cursor validation, whitespace-safe path
  scanning, and trusted-source excerpt/privacy reconstruction distinct from
  standalone internal-consistency validation.
- Closed independent-review blockers by reusing v6.4 request normalization and
  closed status/reason maps, fixing the GET endpoint to production-readonly
  paths, and redacting common absolute-path forms before evidence identity.
- Added standalone/internal-consistency and core/source-authenticity boundary
  documentation plus the exact third-party `budget_policy` identity transform.
- Added negative coverage for rehashed invalid time/budget/status/reason,
  anchor URI, duplicate evidence, extra fields, classification references, and
  Windows/UNC/POSIX/file-URI output privacy.
- Added the strict canonical `v7-memory-package-1` JSON protocol and a
  deterministic inference-free text renderer over sealed v6.4 retrieval.
- Bound each evidence item independently to its source conversation, message
  anchor, time, hit type, explanation, and evidence identity; core generation
  verifies those bindings against the query-only source database.
- Reserved stable fact, preference, decision, constraint, open-question,
  historical-state, and conflict slots while requiring all current retrieval
  output to remain `unclassified_evidence`.
- Propagated v6.4 time, keyword, semantic, graph, unavailable, and degradation
  states without representing missing capabilities as complete.
- Added fixed-path read-only CLI and GET previews, future token-budget fields,
  strict malformed/tampered package rejection, and no persistence or sidecar
  initialization path.

## v6.4 - Graph-Enhanced Retrieval

- Sealed `v6.4-retrieval-2`: bounded-time queries now fail closed before
  conversation-summary semantic search, preventing out-of-window summaries
  from becoming semantic or graph seeds.
- Bound every keyword, semantic, and graph evidence item to an independent
  immutable message source; merged result anchors are rebuilt from the primary
  evidence instead of the newest unrelated message.
- Made the context-character budget absolute for every value, including
  `0..3`, with clipping markers counted inside the limit.
- Split semantic dependency, model, missing sidecar, corrupt SQLite, invalid
  record/schema, and unprovable-time failures into stable reason codes.
- Added one read-only retrieval contract combining keyword messages,
  compatible semantic summaries, and receipt-validated v6.3 graph relations.
- Added inclusive start/end filtering that applies to direct hits and every
  semantic/graph anchor, preventing relation paths from leaking content outside
  the requested interval.
- Added snippet and conversation-anchor modes, merged multi-evidence results,
  deterministic direct-before-graph ranking, conversation diversity caps,
  stable cursors, and strict result/relation/path/context budgets.
- Added explicit independent degradation for missing or invalid semantic and
  knowledge sidecars without initialization, file creation, or production
  database writes.

## v6.3 - Knowledge Graph Query and Explanation

- Closed the independent-review blockers: explicit node-scope filters now
  return empty results when they match no seed, and formal relations require a
  complete v6.2 receipt-backed persistence, transaction, store, candidate,
  plan, and decision identity chain.
- Added bounded read-only knowledge graph queries by conversation, memory
  space, relation type, node, and path depth through a local CLI and GET API.
- Kept active v6.2 formal relations, v6.0 deterministic edges, semantic
  candidates, and unavailable states in separate result classes with exact
  node-version and evidence bindings.
- Added redacted protocol explanations and related-summary aggregation without
  source text, embeddings, raw metadata, paths, prompts, or new inference.
- Missing or empty production knowledge state remains non-mutating and is
  reported accurately; production sidecar and Review Authority initialization
  remain out of scope.

## 2026-07-12

- Closed the v6.2 independent-review blockers. Persistence now rebuilds current
  discovery through sealed fixed-source providers and binds confirmation to a
  random immutable knowledge store identity, generation, initialization seal,
  schema fingerprint, and metadata digest.
- Added an Authority reservation that locks registry then ledger and holds the
  validated mutation head through final knowledge commit. Receipts and backups
  now bind the complete store, Authority head, and anchor assertion.
- Restore now requires a state-bound confirmation and uses the same per-store
  coordination lock as persistence. Subprocess hard-exit tests cover rollback,
  post-commit convergence, and automatic lock release.

- Added the v6.2 Controlled Knowledge Persistence Gateway. It reopens the
  current Review Authority and revalidates candidate, plan, accepted decision,
  supersession state, current graph build, endpoint payloads, evidence, and
  exact knowledge schema before any controlled test write.
- Added append-only versioned accepted relations and transaction receipts,
  SQLite-consistent backups with checksum-bound manifests, idempotent retry,
  pre/post-commit interruption recovery, verified restore, schema fingerprints,
  and explicit database-connection release coverage.
- Reserved a fail-closed external durable-anchor interface and production
  enablement checklist. Production initialization and persistence remain
  disabled; the CLI is read-only in effect, all write tests use the OS temporary
  root, and no external service or network is used.

- Added a backup-first ChatGPT incremental importer that appends only stable-ID
  conversations and messages strictly newer than the existing conversation
  cutoff, preserves extension tables, verifies FTS/foreign keys/duplicates, and
  commits in one transaction. The completed import added 231 conversations and
  10,407 messages, producing the verified real-database state of 409
  conversations and 16,324 messages.
- Updated the read-only audit baseline to 408 non-DeepSeek conversations and
  15,636 non-DeepSeek messages; the existing 1 conversation and 688 messages
  from DeepSeek continue to be checked as a separate provenance slice.

- Closed the final Review Authority isolation gaps: production CLI authority
  selection is fixed and cannot be redirected by test environment variables;
  the test factory is private, handle construction is disabled, and issued
  handles are tracked as process capabilities without an importable sentinel.
- Upgraded the authority schema to `v6.1-authority-2`. Every open and mutation
  now validates SQLite integrity and foreign keys, every stored candidate,
  chain, session, plan, decision, and supersession, plus complete mutation-event
  coverage and digest continuity. Direct unsealed rows fail closed.
- Added one-to-many review-chain predecessor bindings. A later graph build now
  stales every active chain in the same relation/direction/endpoints family,
  while same-build replacement alternatives remain independently reviewable.

- Replaced caller-selectable review stores and session tokens with sealed
  `ReviewAuthorityHandle` instances issued only by fixed production or explicit
  OS-temporary test factories. Plans, decisions, and supersessions now bind the
  authority store, generation, metadata seal, protocol, session, and unique
  review chain.
- Split authority identity into a registry and append-only ledger with matching
  mutation heads. Missing, substituted, schema-modified, or rolled-back ledgers
  fail closed; production authority paths are reserved but remain absent and
  have no v6.1 initialization entry point.
- Added exact SQLite schema fingerprints, protocol allowlists, cross-record
  reference validation, review-chain uniqueness, rollback/corruption probes,
  and cross-open CLI decision-conflict coverage. Replacement preview now says
  `would_supersede` and records no coordination state.

- Closed the v6.1 independent-review blockers with one authoritative temporary
  review-state ledger. Public prepare/verify/decide paths now require a bound
  session; atomic decision append makes identical retries idempotent and rejects
  accept/reject/defer/replace conflicts across separate CLI calls.
- Added append-only supersession records and a deterministic same-relation,
  same-direction, same-endpoints replacement policy. Replace confirmation and
  decision envelopes bind the replacement candidate, replacement plan, both
  payload hashes, evidence, graph build, and policy; old plans immediately
  verify as superseded and cannot decide again.
- Hardened first-append identity checks for temporary candidate, plan, decision,
  and supersession records, with concurrent SQLite coverage and explicit
  Windows connection-release tests. Coordination remains system-temporary and
  `persisted=false`; no production review or knowledge sidecar was added.

- Added YunOS v6.1 relation candidate discovery over trusted v6.0 graph builds.
  Stable IDs bind normalized endpoints, node payloads, generator/configuration,
  evidence digest, score/threshold, status, and graph-build identity without
  text, vectors, raw metadata, paths, timestamps, or query-order dependence.
- Added deterministic review plans, full-field stale revalidation, safe reviewer
  digests, explicit accept/reject/defer/replace decision envelopes, immutable
  replacement previews, idempotent decision identity, and conflict-aware
  system-temporary test storage. All outcomes remain read-only and unpersisted.
- Added the read-only `review_memory_graph_candidates.py` CLI with strict bounded
  listing and current-state prepare/verify/decide/replacement flows. There is no
  production write, initialization, bulk decision, automatic acceptance,
  scheduler, daemon, external model, or arbitrary candidate injection path.

## 2026-07-11

- Added YunOS v6.0 Evidence-backed Memory Graph Foundation. Stable,
  source-qualified nodes now cover conversations, summaries, and memory
  spaces; deterministic edges cover ownership and consecutive sidecar
  versions; stored compatible embeddings can produce threshold-qualified,
  undirected semantic candidates with complete generator/config/evidence
  identities.
- Added a read-only, metadata-only graph preview with conversation, memory
  space, relation, score, legacy, and bounded sample filters. Missing, stale,
  invalid, or provider-incompatible embeddings are reported as unavailable
  evidence rather than fabricated relationships.
- Added an append-only knowledge schema and repository restricted to new
  system-temporary test databases, plus fixed-path production status
  inspection. The production knowledge and memory sidecars remain absent and
  no production database is initialized or written by v6.0.
- Hardened the v6.0 identity seal: semantic candidates now require exact,
  type-checked embedding metadata for source, conversation, version, hashes,
  scope, provider, and dimension, while conversation nodes use one immutable
  scope-free payload across builds. Incomplete legacy metadata fails closed as
  unavailable evidence.

## 2026-07-11

- Closed the final v5.4 verification-gate blocker: `status`, no-action semantic
  prepare, post-execution verification, and explicit verify now share the only
  code path allowed to emit `completed`. Metadata damage/misbinding, provider
  incompatibility, retrieval failure, and old-version embeddings return an
  actionable non-complete state without writes, backups, or logs.

## 2026-07-11

- Added YunOS v5.4 Controlled Memory Refresh Pipeline. A single-conversation,
  read-only-by-default CLI now composes change detection, candidate preview,
  separately confirmed formal-summary acceptance, separately confirmed
  semantic maintenance, and final binding/retrieval verification. The semantic
  lifecycle prefers latest accepted sidecar versions over legacy summaries and
  uses source-qualified identity to prevent cross-source ID collisions.
- Added recoverable `summary_accepted_index_pending`, `semantic_plan_ready`,
  stale, busy, retry, completed, and manual-review outcomes without claiming a
  cross-database transaction. Controlled semantic plans bind the exact accepted
  version/hash, candidate, acceptance plan, source snapshot, provider identity,
  and target kind. Busy/no-action paths leave no backup or partial write.
- Added system-temporary end-to-end tests for idempotency, interrupted-stage
  recovery, failed-index retry, stale old plans, independent confirmations,
  legacy fallback, source-ID collisions, privacy-safe audit output, semantic
  retrieval, unchanged source hashes, and immediate Windows file deletion.

## 2026-07-11

- Hardened v5.3 production execution after final review: controlled-summary
  paths now reject hard-link and reparse aliases to every production database;
  Windows source reads deny concurrent writable opens; supported sidecar
  writers are serialized around transaction-bound candidate/base revalidation;
  busy and stale outcomes remain write- and backup-free; and idempotent success
  requires the stored acceptance plan ID to match.
- Finalized YunOS v5.3 Controlled Summary Acceptance. Versioned summaries now
  live in the independent append-only `data/memory/ayunos_memory.db` sidecar;
  prepare is read-only and execute requires a complete revalidated confirmation
  envelope, one candidate per run, and a pre-write SQLite backup. v5.1 change
  detection prefers the latest accepted sidecar coverage, including
  `source_snapshot_hash`, before legacy fallback. No production sidecar was
  initialized, no real candidate was accepted, and no embedding lifecycle runs
  automatically.

## 2026-07-11

- Finalized YunOS v5.2 controlled summary candidate preview. The read-only CLI
  defaults to five metadata-only candidates, requires `--show-text` for正文,
  caps `--limit` at 50, and binds each candidate to its source snapshot and
  deterministic generator identity/configuration. No formal summaries,
  sidecar, embeddings, candidate files, or external models are used. Current
  debt: 177 `summary_missing`, 1 `coverage_unknown`; confirmation/write-back
  remains the next stage.

## 2026-07-11

- Sealed YunOS v5.1 read-only Memory Change Detection. Deterministic message
  snapshots classify missing, changed, unknown, orphaned, and current summary
  coverage without writing the real database, production sidecar, summaries,
  or embeddings. The v5.2 handoff is candidate generation only.

## 2026-07-11

- Finalized YunOS v5.0.1 Execution Hardening: immutable target-bound plan envelopes, production Executor confirmation enforcement, strict SQLite error classification, all-or-nothing rollback for non-unique failures, accurate committed/rolled-back/busy propagation, unified plan identity across backups and audits, and backup restore coverage. Check and prepare remain read-only; the real chat database remains read-only.
- Closed remaining production write bypasses: direct semantic record writes are refused for the production sidecar, raw transaction entry is private, the gateway revalidates the complete confirmation envelope, stale execution uses unified audit output, and gateway tests explicitly close SQLite connections.

## 2026-07-10

- Sealed YunOS v5 Memory Lifecycle: Planner, controlled Executor, confirmed Orchestrator, production semantic sidecar, transactional maintenance, and one-shot lifecycle entrypoint. Deferred keyword, hybrid, and evaluation technical debt remains out of scope.

## 2026-07-10

- Added YunOS v5-E transactional production semantic sidecar maintenance with pre-write backups and immediate SQLite write locking.

## 2026-07-10

- Added YunOS v5-D production semantic sidecar lifecycle: exact-path sidecar protection, explicit write confirmations, and real chat database read-only isolation.

## 2026-07-10

- Added YunOS v5-A read-only Memory Lifecycle Planner foundation. It classifies summary-to-embedding state and recommends future actions without creating summaries, embeddings, or database writes.

## 2026-07-10

- Sealed YunOS v4.0.1 Semantic Memory Hardening: controlled test-database writes, immutable local BGE identity, unified semantic retrieval errors with explicit `legacy_fallback`, and exact legacy/E2E retrieval tests. Default legacy behavior remains unchanged; v5 is the next line.

## 2026-07-10

- Sealed YunOS v4-F1 controlled hybrid retrieval preview: existing keyword retrieval plus local BGE semantic summary retrieval, fused with RRF `k=60`.
- Sealed YunOS v4-F2 synthetic hybrid evaluation: keyword/semantic/hybrid Hit@3 `0.75/1.00/1.00`; natural/short keyword nonempty `0.50/1.00`; mixed-source rate `0.75`; keyword gap `true`; quality gate passed.
- Sealed YunOS v4-F3 controlled runtime integration: explicit `legacy|hybrid`, legacy default, one Hybrid Relevant Memory section, and `legacy_fallback` for missing indexes or model mismatch. Real acceptance counts were keyword/semantic/combined `0/2/2`.
- YunOS v4 is formally closed. The next planned line is v5.

## 2026-07-07

- Added YunOS v4-A semantic memory foundation: schema notes, deterministic local embedding provider, dry-run CLI, and tests. No real database migration, provider API call, or runtime retrieval integration is enabled.
- Added YunOS v4-B test-database semantic memory migration, deterministic test embedding writes, and read-only test embedding search. Real database migration and runtime retrieval integration remain disabled.
- Added YunOS v4-C summary-only semantic index builder from `memory_summaries` into a test semantic database. Raw message indexing and runtime retrieval integration remain disabled.
- Added YunOS v4-D read-only semantic summary search preview for deterministic test embeddings through CLI and a GET-only local API. Production semantic retrieval and whale runtime integration remain disabled.
- Added YunOS v4-E1 local-only `BAAI/bge-small-zh-v1.5` embedding provider with CPU default and an E-drive model cache. Runtime retrieval remains disabled.
- Added YunOS v4-E2 synthetic local embedding quality evaluation. It compares deterministic test vectors with the approved local BGE model without user data or LLM judging.
- Added YunOS v4-E3 local BGE summary-only indexing into a gitignored test database. The real database remains `memory_summaries` read-only and runtime retrieval remains disabled.
- Finalized YunOS v4-E local embedding evaluation documentation. v4-F Hybrid Retrieval remains out of scope.

## 2026-07-05

- Sealed YunOS v3 Memory Summarizer documentation.
- Recorded v3-A through v3-F completion status.
- Recorded current real local database counts: `messages=5917`, `deepseek+whale messages=688`, `memory_summaries=2`.
- Documented the v3 safety boundary: no automatic message rewriting, no automatic LLM/API calls, no automatic summary refresh, and continued `whale` / `general` memory-space separation.
- Added `docs/memory_summarizer_v3.md` as the v3 seal note.

## 2026-06-04

- 初始化 AyunOS 项目结构
- 创建基础文档
- 创建 Git 忽略规则
- 添加目录占位文件
