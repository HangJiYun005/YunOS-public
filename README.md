# YunOS

Sanitized source snapshot. Private databases, attachments, credentials, local configuration, and original Git history are excluded. Historical deployment reports are placeholders, not current acceptance evidence. See [sanitization notes](SANITIZATION.md).

YunOS is a local-first memory workspace for exported AI conversations. This repository contains the public, reproducible project structure for the current local line, with source code, public documentation, mock demo materials, tests, and schema notes.

The private conversation database is not part of the repository. Raw exports, backups, logs, private config, and real chat records must stay local.

## 当前版本：YunOS v10 — 受限认知代理

YunOS v10 在 v7 Memory and Evidence、v8 Trusted Runtime 和 v9 Operational State 之上，
增加 Objective、Plan、Provider、Capability、Authorization、Tool Gateway、Supervision、
Verification、Acceptance 与 Learning Candidate。它复用既有五个 SQLite 数据库，不创建
第二套 Runtime、Operational State、Evidence System 或第六个数据库。

Omnibox 的 Search 与 AI 是两条独立链路：

- Search 完全本地，不调用模型、不产生 API 费用、不向外发送内容，并保留移动端与离线读取；
- AI 只向用户手动选择的 Provider 发送有来源、有预算的最小必要上下文；
- 模型只能回答、规划或提出 typed tool proposal，不能直接拥有 shell、文件系统、Git、
  数据库、网络或凭据；
- 普通 Web、Tailnet 与移动端保持只读，Act 写入必须经过不暴露给 Tailnet 的本机 local
  broker/CLI 和独立 Authorization Artifact；
- 生产默认 `real-readonly`，writer lease 保持为零。

v10.0 至 v10-E 已形成节点关闭证据，v10-F 是当前且唯一的生产接纳与最终封板节点。在最终
全量、恢复包、本地 `main --ff-only` 和 annotated tag 全部完成前，不提前声明
`YUNOS_V10_FINAL_SEAL_GO`。v10-F 后停止，不创建 v10-G、v10.1 或 v11，也不激活
Historical Reconstruction 或 `v8302`。

当前架构、权威路线和专题文档入口见 [`docs/architecture.md`](docs/architecture.md)、
[`docs/design/yunos_authoritative_roadmap.md`](docs/design/yunos_authoritative_roadmap.md) 与
[`docs/README.md`](docs/README.md)。

## What v2 Can Do

- Import ChatGPT-style exports into a local SQLite memory database.
- Preserve source provenance with `source_platform` values such as `chatgpt`, `deepseek`, `claude`, `gemini`, `manual`, `markdown`, and `txt`.
- Search local conversations and jump back to source messages with conversation/message references.
- Run a local Web UI for search, conversation detail, timeline, project views, tags, marks, and source trace review.
- Use a sanitized test database by default for development, demos, and repository validation.
- Open the real local database only through explicit real-mode commands.
- Keep real Web access read-only with `web-real-readonly`.
- Support separate `memory_space` scopes for `whale` and `general`.
- Retrieve whale-scoped memory without falling back to the whole local corpus.
- Run smoke, privacy, project, and audit checks before staging.

Search mode still does not include cloud sync, background import, automatic LLM summary refresh, or required Provider API calls. AI mode is separate and may call a manually selected Provider only through its explicit context, budget, Runtime receipt, and authorization boundaries.

Lifecycle production writes are restricted to the confirmed sidecar gateway: direct record writes are refused, plans are revalidated immediately before execution, and test maintenance closes all SQLite connections before cleanup. This hardening does not claim resistance to malicious same-process access to private functions.

## YunOS v3 Memory Summarizer

YunOS v3 is sealed as a read-mostly Memory Summarizer layer:

- v3-A: summarizer scaffold.
- v3-B: controlled test database write.
- v3-C: real database safe append with explicit safety gates.
- v3-D: read-only summary viewer.
- v3-E: summary health diagnostics.
- v3-F: whale_chat summary context injection.

Local production statistics are omitted from this sanitized snapshot.

v3 does not automatically rewrite original `messages`, call LLM/API providers, refresh summaries, or collapse `whale` and `general` memory spaces. `whale_chat.py` only reads existing whale summaries as high-level context.

See `docs/memory_summarizer_v3.md` for the sealed v3 status, safety boundaries, verification set, and v3.1/v4 recommendations.

## YunOS v4 Semantic Memory Foundation And Hybrid Retrieval

YunOS v4 is sealed as a staged, local-first semantic memory foundation:

- v4-A: schema, deterministic test embeddings, provider abstraction, and dry-run CLI.
- v4-B: test semantic database migration, writes, duplicate handling, and read-only query.
- v4-C: summary-only index construction from `memory_summaries` into a test database.
- v4-D: read-only semantic summary search preview through CLI and GET-only local API.
- v4-E: local `BAAI/bge-small-zh-v1.5` provider, synthetic quality evaluation, and test summary index.
- v4-F1: controlled hybrid preview combining keyword retrieval with BGE summary retrieval using RRF with `k=60`.
- v4-F2: synthetic evaluation of keyword, semantic, and hybrid coverage.
- v4-F3: explicit `legacy|hybrid` prompt preview mode with legacy default and safe `legacy_fallback`.
- v4.0.1-A: controlled `data/test` write boundary with link, reparse-point, and hardlink rejection.
- v4.0.1-B: immutable embedding identity with local BGE revision `7999e1d...`, fingerprint `1854989a20e6`, protocol `sentence_transformers_normalized_v1`, and dimension `512`.
- v4.0.1-C: unified corrupted semantic retrieval errors and explicit legacy fallback warnings.
- v4.0.1-D: exact legacy golden regression and real-function E2E retrieval evaluation.

The v4 boundary is deliberate: keyword retrieval is unchanged, hybrid mode is opt-in, semantic records remain summary-only, and real data access stays read-only. No cloud API or LLM call is required, and no runtime setting is persisted automatically.

v4 is formally closed. The next planned line is v5.

See `docs/semantic_hardening_v4_0_1.md` for the v4.0.1 hardening seal, verification boundary, and v5 handoff.

## YunOS v5 Memory Lifecycle (Sealed)

YunOS v5 is sealed as a controlled, summary-only memory lifecycle:

- v5-A/A.1: read-only planning with current embedding priority and two real summaries up to date.
- v5-B: dry-run-first, idempotent create/rebuild execution that appends versions without deleting history.
- v5-C: deterministic plans, explicit confirmation, stale-plan rejection, and post-execution verification.
- v5-D: the production sidecar is `data/semantic/ayunos_semantic.db`; the chat database remains read-only.
- v5-E: transactional sidecar maintenance with backup, rollback, concurrency rejection, and audit output.
- v5-F: one-shot `check` / `prepare` / explicitly confirmed `execute` entrypoint; no scheduler or daemon.

Lifecycle maintenance never reads `messages`, rewrites summaries, calls external APIs/LLMs, or connects to whale_chat/Web. Keyword-gap, hybrid-fusion, and expanded evaluation coverage remain future technical debt.

See `docs/memory_lifecycle_v5.md` for the sealed scope and safety contract.

### YunOS v5.2 Summary Candidate Preview

v5.2 generates deterministic, read-only summary candidates from v5.1 change
detection. The default preview returns metadata for up to five conversations;
candidate text requires explicit `--show-text` and the maximum limit is 50.
Candidates bind the source snapshot, generator identity, and generator
configuration. They never write formal summaries, the production sidecar,
embeddings, candidate files, or logs. The current real debt is 177 missing
summaries and 1 coverage-unknown item; candidate confirmation and controlled
write-back are deferred to the next stage.

See `docs/summary_candidate_preview_v5_2.md` for the safety contract and CLI.

### YunOS v5.3 Controlled Summary Acceptance

v5.3 accepts one explicitly confirmed v5.2 candidate at a time into the
independent append-only memory sidecar `data/memory/ayunos_memory.db`. The
original chat database remains permanently read-only, and the production
memory sidecar is not initialized automatically.

`prepare` is read-only: it regenerates the candidate, binds a deterministic
acceptance plan to the source snapshot, generator configuration, candidate
hash, and base-summary state, and creates no database, backup, or audit log.
`execute` requires the candidate ID, plan ID, and all production confirmations;
it rejects test source/sidecar aliases to every production database using file
identity and reparse checks. During execution, the source is held read-only
against concurrent writable opens while sidecar writers are serialized; the
candidate, generator configuration, and base summary are revalidated under the
sidecar write protocol immediately before the append-only transaction. Stale,
refused, busy, and repeated candidate requests do not create backups; a
successful write creates a SQLite backup. Accepted versions are never updated
or deleted. Repeated candidate IDs are idempotent only when the stored plan ID
also matches.

v5.1 change detection prefers the latest accepted sidecar version, then falls
back to legacy `memory_summaries`. Coverage agreement includes the complete
`source_snapshot_hash`; v5.3 does not generate embeddings or run semantic
lifecycle maintenance. Real acceptance has not initialized the production
memory sidecar and has not accepted a real candidate.

See `docs/controlled_summary_acceptance_v5_3.md` for the sealed workflow and
verification boundary.

### YunOS v5.4 Controlled Memory Refresh Pipeline

v5.4 composes v5.1 change detection, v5.2 candidate generation, v5.3 formal
summary acceptance, and the hardened semantic lifecycle into one recoverable
workflow for exactly one conversation. The default `status` phase is read-only;
summary acceptance and semantic maintenance remain two separate production
writes with different plan IDs and different confirmation flags.

The semantic lifecycle now selects the latest accepted `summary_versions`
record from the memory sidecar first, then the latest legacy
`memory_summaries` record. Semantic identity uses `source_table + source_id`,
so a legacy ID cannot collide with a sidecar summary ID. Controlled v5.4 plans
also bind summary source, conversation, version, text hash, memory space,
candidate ID, acceptance plan ID, source snapshot hash, complete provider
identity, and semantic target kind. Those fields are revalidated before any
write and are stored in existing semantic metadata; neither production schema
is changed.

Run one explicit phase at a time:

```powershell
python tools/run_memory_refresh.py --conversation-id <id>
python tools/run_memory_refresh.py --phase prepare-summary --conversation-id <id>
python tools/run_memory_refresh.py --phase execute-summary --conversation-id <id> --confirm-candidate-id <id> --confirm-acceptance-plan-id <id>
python tools/run_memory_refresh.py --phase prepare-semantic --conversation-id <id>
python tools/run_memory_refresh.py --phase execute-semantic --conversation-id <id> --confirm-semantic-plan-id <id>
python tools/run_memory_refresh.py --phase verify --conversation-id <id>
```

Production execute phases additionally require their existing, phase-specific
production gates. No command initializes the production memory sidecar,
accepts a candidate, or writes an embedding automatically. Candidate text is
absent unless `--show-candidate-text` is explicitly supplied, and it is never
written to workflow audit output or logs.

`completed` is emitted only by the shared full-verification gate. `status`, a
no-action semantic prepare, post-execution verification, and explicit `verify`
all require the accepted summary, current change coverage, current lifecycle,
exact semantic metadata/provider binding, and an actual retrieval hit. A
metadata or retrieval failure returns a repair or manual-review action instead
of a simplified success.

See `docs/controlled_memory_refresh_v5_4.md` for states, recovery semantics,
confirmation envelopes, and verification coverage.

## YunOS v6.0 Evidence-backed Memory Graph Foundation

v6.0 adds a deterministic, evidence-bound graph preview over existing formal
summary identities. It normalizes `conversation`, `summary`, and
`memory_space` nodes; emits deterministic ownership/version edges; and creates
only threshold-qualified, undirected `semantic_related` candidates from
already stored compatible embeddings. It does not extract free-form facts,
guess people or causal relationships, confirm candidates, or call a model.

Summary discovery stays on the existing repository path: all accepted memory
sidecar versions take precedence, while the latest legacy conversation summary
is used only as a per-conversation fallback. `--include-legacy` makes the
otherwise superseded legacy identity visible for review. Node and edge IDs bind
their schema, source-qualified identity, version/hash evidence, generator
configuration, and (when applicable) score and threshold.

Semantic graph edges fail closed unless stored embedding metadata exactly
matches the summary source, conversation, version, text/snapshot hashes, and
memory space. Missing or legacy-incomplete metadata is reported as unavailable,
never guessed. Conversation nodes keep a scope-free immutable payload;
memory-space membership remains on summary nodes and explicit edges.

The production knowledge sidecar is reserved at
`data/knowledge/ayunos_knowledge.db`, with future backups reserved under
`backups/knowledge/`. v6.0 deliberately creates neither location nor database.
Schema initialization and append-only repository writes are available only for
new databases under the system temporary directory. Production inspection is
status-only and read-only.

Run the safe preview:

```powershell
python tools/preview_memory_graph.py --limit 5 --json
```

The CLI has no execute, write, or accept flag. Output is capped at 20 records
per sample and excludes summary/message text, embeddings, internal prompts,
absolute paths, and full exception stacks. See
`docs/memory_graph_foundation_v6_0.md` for the identity envelope, relation
rules, schema boundary, filters, tests, and v6.1 handoff.

## YunOS v6.1 Relation Candidate Review (Awaiting Independent Review)

v6.1 builds the human-review layer promised by v6.0. Trusted graph builds yield
stable, evidence-bound relation candidates; deterministic review plans bind the
candidate, both node payloads, generator/configuration, evidence, score,
threshold, graph build, and review policy. Human `accept`, `reject`, `defer`,
and `replace` decisions require a complete confirmation object and are
revalidated against a freshly rebuilt candidate before a non-persisted decision
envelope is emitted.

The default CLI is read-only `list`. `prepare`, `verify`, `decide`, and
`replacement-preview` require a process-issued handle from the fixed production
Review Authority factory; no environment variable or public test factory can
redirect the CLI to another authority. All commands report `persisted=false`, meaning no production graph
relationship was written. The production registry/ledger paths are reserved
under `data/review`, but v6.1 has no production initializer and does not create
that directory. Tests use complete authority replicas below the OS temporary
root through private test support. Every open verifies SQLite integrity,
foreign keys, all stored protocol records and references, and the complete
mutation-event chain. An authority-bound plan can have only one decision; retries are
idempotent and different decisions conflict across process opens.
`accept` means only `accepted_pending_persistence`.
Deterministic, unavailable, stale, or superseded relations cannot be accepted.
No production review repository or knowledge sidecar is initialized or written.

Replace decisions atomically supersede the original plan and bind a separately
prepared replacement plan. The replacement policy preserves relation type,
direction, and normalized endpoints; replacement confirmation and envelopes
include both replacement payload hashes and the replacement plan identity.

```powershell
python tools/review_memory_graph_candidates.py --limit 5 --json
```

See `docs/relation_candidate_review_v6_1.md` for identity fields, stale rules,
confirmation requirements, replacement semantics, privacy boundaries, test
repository restrictions, and the v6.2 handoff.

## YunOS v6.2 Controlled Knowledge Persistence Gateway (Awaiting Independent Review)

v6.2 adds a separate, versioned gateway for converting exactly one current
`accepted_pending_persistence` Review Authority decision into an append-only
formal relation and transaction receipt. The gateway reloads and validates the
authority registry/ledger, candidate, plan, decision, supersession state,
current graph build, both graph-node payloads, evidence, and the initialized
knowledge schema. It rebuilds discovery through a sealed provider: production
uses only fixed read-only chat, memory, and semantic paths, while tests use a
sealed source below the OS temporary root. Callers cannot supply discovery.

Each write takes an exclusive per-store lock and an Authority registry-to-ledger
reservation held through knowledge commit. It creates a SQLite-consistent
backup, rebuilds and revalidates all facts again, and commits the
relation version and receipt in one `BEGIN IMMEDIATE` transaction. Candidate,
plan, decision, chain, and transaction uniqueness make retries idempotent.
Execution also requires the caller to return the exact deterministic
`persistence_id` emitted by the preceding read-only preview. That ID binds the
Authority mutation head, anchor assertion, and the knowledge store's random ID,
generation, initialization seal, schema fingerprint, and metadata digest.
Pre-commit interruption rolls back; post-commit interruption converges on the
existing receipt; a verified restore API can return a test sidecar to the exact
pre-transaction backup. Restore uses the same per-store lock and a separate
state-bound restore confirmation. The schema is append-only and
fingerprint-checked.

This implementation does **not** enable production persistence. Initialization,
writes, backups, restores, and end-to-end tests are restricted to existing
databases and directories below the OS temporary root with explicit test mode.
The production CLI is status-only in effect and refuses `persist` with
`production_enablement_incomplete`; it has no initialization or restore
command and accepts no database paths. Production review, memory, knowledge,
chat, and semantic files remain unchanged.

```powershell
python tools/persist_knowledge_relation.py status
python tools/persist_knowledge_relation.py persist --candidate-id <id> --review-plan-id <id> --decision-id <id>
```

The second command is deliberately refused until a separately approved
production initializer, explicit operator authorization, validated backup and
restore location, durable external authority anchor, matching anchored mutation
head, and independent security/recovery review all exist. v6.2 defines the
external-anchor assessment interface and fail-closed gate only; it performs no
network call and does not claim those conditions are met. See
`docs/controlled_knowledge_persistence_v6_2.md`.

## YunOS v6.3 Knowledge Graph Query and Explanation

v6.3 provides bounded read-only queries over the sealed knowledge sidecar by
conversation, memory space, relation type, node, and path depth. Active v6.2
formal relations, v6.0 deterministic edges, semantic candidates, and
unavailable records are returned in separate groups. Candidates are excluded
by default and are never promoted from Review Authority decisions or transient
discovery state.

Every returned relation binds its node identities, schema/relation version,
generator configuration, and evidence identity. Formal relations additionally
require a matching v6.2 receipt and validated persistence/transaction identity.
Paths use deterministic ordering, never repeat a node, and are limited to
depth 3 and a relation/path budget of 100; endpoint nodes form a separately
bounded verification closure.
Explanations are fixed protocol descriptions: no source text, embeddings, raw
metadata, absolute paths, prompts, or generated facts are exposed.

```powershell
python tools/query_knowledge_graph.py --conversation-id <id> --max-depth 2 --json
```

The local GET API is `/api/knowledge-graph-query`. Both surfaces use only the
fixed production knowledge path in SQLite read-only mode. If the sidecar is
absent, v6.3 returns `knowledge_sidecar_not_initialized` without creating it.
See `docs/knowledge_graph_query_v6_3.md`.

### v5.0.1 Execution Hardening

v5.0.1 closes the production execution review blockers without expanding lifecycle scope: confirmed plans now bind target kind, complete provider identity, action, and content hash; the Executor rechecks the confirmed envelope and source hash before maintenance; SQLite errors distinguish unique duplicates, busy/locked, and rollback failures; and execution, backup, and audit output share one plan ID. Check and prepare remain read-only, and the real chat database remains read-only.

See `docs/execution_hardening_v5_0_1.md` for the verification boundary and residual limitations.

## Memory Spaces

YunOS keeps provenance and memory scope separate:

- `source_platform` describes where a record came from, for example `chatgpt` or `deepseek`.
- `memory_space` describes which memory scope a record belongs to, currently `whale` or `general`.

The whale retrieval path is intentionally narrow. Missing or empty memory-space input should not broaden retrieval to all local history.

## Real Readonly Web Mode

The normal Web command uses the sanitized test database:

```powershell
.\run.ps1 web
```

To inspect the real local database from the Web UI, use the explicit read-only command:

```powershell
.\run.ps1 web-real-readonly
```

This mode opens the real SQLite database with read-only intent and prints a warning before starting. It is for inspection only.

## Local Setup

Create a project-local Python environment:

```powershell
<python> -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the project-local command wrapper:

```powershell
.\run.ps1 smoke
.\run.ps1 scan
.\run.ps1 audit
```

If PowerShell blocks script execution, use a one-time process override:

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 smoke
```

## v6.4 Graph-Enhanced Retrieval

Use the unified, read-only keyword + semantic + knowledge-graph search:

```powershell
python tools/graph_enhanced_search.py "search terms" --json
python tools/graph_enhanced_search.py "search terms" --start-time 2025-01-01 --end-time 2025-12-31 --result-mode conversation_anchor --graph-depth 2 --json
```

The equivalent local endpoint is
`GET /api/graph-enhanced-retrieval?q=...`. Time bounds are inclusive and
default to all time. Missing semantic or knowledge sidecars degrade explicitly
without file creation. See `docs/graph_enhanced_retrieval_v6_4.md`.

## v7.0 Memory Package Contract

Convert v6.4 results into deterministic, model-independent canonical JSON or a
faithful text preview:

```powershell
python tools/memory_package_preview.py "search terms" --json
python tools/memory_package_preview.py "search terms" --future-token-budget 4096
```

The equivalent read-only endpoint is
`GET /api/memory-package-preview?q=...&format=json|text`. It is available only
when the Web process uses the exact production main database in read-only mode;
it never inherits another `--db` value. Public entry points accept no database
path. Every item stays `unclassified_evidence`; future fact,
preference, decision, constraint, open-question, historical-state, and conflict
slots remain empty. Missing semantic or knowledge capability is propagated as
an explicit degraded/partial state. v6.4 production and v7 validation share one
cross-field retrieval state machine, including derived page counts and cursors.
Graph expansion additionally uses a shared direct-seed derivation: only actual
keyword/semantic hits can reach the graph branch. Graph-only requests fail with
`graph_requires_direct_channel`; every graph item binds its seed conversation,
message, complete canonical direct hit-type set, and path start to evidence in
the same envelope. `no_direct_seed` requires an executed direct channel and
cannot coexist with any evidence.
Windows, UNC, POSIX, and file-URI absolute paths are scanned across spaces and
replaced before identity calculation while surrounding punctuation is kept.
Standalone validation proves internal consistency only; core source-backed
validation rebuilds trusted message excerpts and privacy from SQLite. See
`docs/memory_package_contract_v7_0.md`.

## v7.1 Evidence Selection and Budgeting

Build a deterministic finite evidence payload without changing the v7.0
package contract:

```powershell
python tools/evidence_selection_preview.py "search terms" --budget small --json
python tools/evidence_selection_preview.py "search terms" --budget custom --character-budget 8000 --estimated-token-budget 2000 --conversation-cap 4 --max-selected 20
```

The production-readonly GET equivalent is
`GET /api/evidence-selection-preview?q=...&budget=small|medium|large|custom&format=json|text`.
Public entry points accept no database path. Same-anchor hits share one excerpt
while retaining every hit provenance; exact role/excerpt equivalents use only
NFC/newline structural normalization. Direct keyword/semantic candidates are
selected ahead of graph-only candidates using deterministic conversation
round-robin and per-conversation caps. If any direct representative is omitted
by the cap, max-items limit, or budget, every graph-only candidate is
`lower_priority`. The `v7.1-evidence-selection-2` package carries a body-free
canonical candidate ledger and a v7.0 source-state projection so standalone
validation can rebuild dedupe, ordering, selection, omissions, source state,
and render cost. Standalone proves only package-internal consistency: a fully
coordinated, privacy-safe repackage may pass because unkeyed identities are not
source authentication. Production CLI/API output is returned only after the
controlled read-only source-backed builder reconstructs v7.0 and v7.1.
Character limits are absolute over the
exact `model_payload`; `unicode-codepoint-ceil-div4-v1` is recorded as a
reproducible planning estimate and never represented as a model tokenizer.
Every omission is body-free and records `duplicate`, `conversation_cap`,
`budget_exceeded`, or `lower_priority`. See `docs/evidence_selection_v7_1.md`.

## v7.2 Temporal Interpretation and Memory Candidates

Convert the validated v7.1 finite evidence set into short, transparent,
reviewable candidates without writing any formal memory:

```powershell
python tools/temporal_memory_candidates_preview.py "search terms" --budget medium --json
python tools/temporal_memory_candidates_preview.py "search terms" --budget medium
```

The production real-readonly GET equivalent is
`GET /api/temporal-memory-candidates-preview?q=...&budget=small|medium|large&format=json|text`.
Neither entry point accepts a database path or interpretation provider. The
protocol `v7.2-temporal-memory-candidates-4` uses built-in local rules for
`fact`, `preference`, `goal`, `decision`,
`constraint`, `acceptable_alternative`, and `open_question`; external provider
calls are rejected before interpretation in this version. A closed attribution
gate accepts only direct user-role expressions and rejects assistant text,
paired quotes, Markdown blockquotes, reports, model advice, `if`/hypothetical
scopes, ordinary or rhetorical questions, examples/code, jokes, and
meta-negated assertions. The scanner runs on the complete expression before
segmentation and carries quote, blockquote, report, example, hypothetical, joke,
and code scopes across segments. Direct open questions are retained only as ambiguous
`needs_review` candidates. Every result remains `state=candidate`,
`readonly=true`, `persisted=false`, and `candidate_only=true`.

Compact structured events preserve evidence IDs, conversation/message anchors,
UTC observation time, explicitness, reason, and confidence. Relationships are
closed to `evolution`, `coexistence`, `conditional`, `supersedes`, `conflict`,
and `unknown` and are derived by topic across compatible candidate types.
Preference is not exclusivity; only a positive exclusive goal plus an explicit
exclusion can supersede. Primary-target polarity is independent of excluded
targets, so excluding Air cannot negate a positive Pro goal. Unknown does not
expire older candidates. Statements are limited
to 120 Unicode code points, reasons to 100, and each selected evidence item to
four events; overflow uses a fixed semantic-safe placeholder and is forced to
`needs_review`.

Timezone interpretation is identity-bound. First use defaults to
`Asia/Shanghai`, revision 1, with `needs_user_confirmation`; all internal times
remain canonical UTC. Country/region is versioned with the timezone and
confirmation state. Later configurations require a validated previous config or
package, increment exactly by one, embed its complete content, and bind the
predecessor identity. Trusted-selection and source-backed validation also require
that previous config or package as independent trusted input; a formatted hash
alone, an empty identity, or an all-zero/all-`f` reserved identity is rejected.
v7.2 writes no timezone config. Standalone
validation proves internal consistency only; production output is gated by
controlled read-only v7.0/v7.1 reconstruction followed by exact v7.2 rebuild;
v7.1 partial/degraded capability state remains explicit in v7.2.
See `docs/temporal_memory_candidates_v7_2.md`.

## v7.3 Context Assembly

v7.3 combines the sealed v6.4 retrieval, v7.0 memory package, v7.1 evidence
selection, and v7.2 temporal candidates into one task-relevant, hard-budgeted
model context. It keeps current task, approved profile, candidate memory, and
historical evidence in separate layers; candidates are always marked
`candidate/not_approved`, while history defaults to citation and message
anchors.

Small, Medium, Large, and bounded Custom profiles independently limit total
characters, estimated tokens, evidence count, candidate count, and complete
history expansions. Whole items are omitted instead of semantically truncated,
and JSON/text previews report exactly what the model saw, what was excluded,
why, and the resulting usage.

Revision 2 propagates empty-but-degraded v7.1 capability state without promotion,
uses exact Latin token and conservative Han-segment relevance (`Pro` cannot
match `project`), and reuses the v7.0 Windows/UNC/POSIX/file-URI privacy boundary
across structured JSON, model context, warnings, and rendered previews.

```powershell
python tools/context_assembly_preview.py "MacBook" --budget medium --json
python tools/context_assembly_preview.py "MacBook" --budget medium
```

The GET-only surface is
`/api/context-assembly-preview?q=...&budget=small|medium|large|custom&format=json|text`.
It requires fixed real-database readonly mode and accepts no database path,
profile input, provider, or persistence destination. Production always performs
source-backed v7.0-v7.3 reconstruction and supplies an empty approved-profile
ledger; v7.3 creates no sidecar and performs no external model call.

See `docs/context_assembly_v7_3.md`.

## v7.4 Transparent Context Review and Delivery

v7.4 wraps one validated v7.3 assembly in a deterministic, versioned delivery
envelope for review and current-request-only adjustment. The “上下文预览” page
shows current task, approved profile, `candidate/not_approved` memory, and
historical evidence as four separate collapsible layers, including source
anchors, omission reasons, character/token estimates, and capability
degradation. An empty production approved-profile input is shown explicitly;
candidates are never promoted into it.

Users may change the query/time range, select Small/Medium/Large/Custom budgets,
or temporarily exclude complete approved/candidate/evidence items and rebuild.
Candidate, evidence, relation, and expanded-history representations declare
typed source/evidence/event provenance. Exclusions resolve a deterministic
provenance closure, so the same source cannot re-enter `model_context` through a
different layer; equal text from an independent source remains untouched. Those
exclusions exist only in the current delivery. They never update profile
approval, candidates, messages, selection state, or a database. Changing an
assembly control clears stale exclusions before the new v7.3 assembly is built.

```powershell
python tools/context_delivery_preview.py "MacBook" --budget medium --json
python tools/context_delivery_preview.py "MacBook" --exclude <item-id>
```

The GET-only endpoint is
`/api/context-delivery-preview?q=...&budget=small|medium|large|custom&exclude=<item-id>&format=json|text`.
It is available only on the fixed real-readonly production source and accepts no
database path, profile/provider injection, persistence destination, or send/model
operation. The `dvy_` identity binds the complete source `ctx_` assembly,
controls, exclusions, final layers, and exact `model_context`. JSON and text
display the same identities. Every envelope marks `persisted=false` and
`sent_to_model=false`; no sidecar or external LLM/API call is made.

Source-message navigation uses the composite `(conversation_id, message_id)`
anchor. Missing, mismatched, absent, or ambiguous combinations fail closed; the
API never falls back to a message-ID-only lookup.

See `docs/transparent_context_delivery_v7_4.md`.

## v8.0-A Lite Approved Profile Control

v8.0-A Lite adds the first user-controlled promotion boundary above the sealed
v7.2 candidate package. The new `画像审批` page can search source-backed v7.2
candidates, inspect their evidence IDs and composite source-message anchor, and
record `approve`, `edit_and_approve`, `reject`, or `defer`. An `open_question`
can only be rejected or deferred. No automatic or shadow approval exists.

Decisions are stored only in an independently initialized append-only SQLite
sidecar. Database triggers forbid update and delete for both decisions and their
immutable replay receipts; the active approved profile
is a deterministic projection of the latest decision for each candidate rather
than a second mutable ledger. Every mutation binds the v7.2 package/candidate/
evidence snapshot, requires the page's `expected_revision`, and carries unique
`request_id` and `mutation_id` keys. Every decision is fixed to
`decision_source=manual`, complete history remains visible, and active items
explicitly carry `model_visible=true`. Exact retries replay the first immutable
receipt; stale pages or reused keys fail with HTTP 409.

Production initialization is deliberately dry-run by default:

```powershell
python tools/init_approved_profile_sidecar.py
```

Actual production initialization additionally requires `--execute`,
`--allow-production-profile-sidecar`, and
`--confirm-production-profile-sidecar-init`, each represented by a strict
boolean flag; non-boolean or partial confirmation is refused. The normal v8.0-A Lite code path
does not create the sidecar. The fixed local API surfaces are:

```text
GET  /api/approved-profile-control?q=...&budget=small|medium|large
GET  /api/approved-profile-active
POST /api/approved-profile-decision
```

GET preview requires the fixed real database in readonly mode. Ordinary
`web-real-readonly` rejects every POST. Approval requires the explicit local
`run.ps1 profile-admin` mode, which still opens the source database read-only.
Every profile-admin POST rebuilds v7.0-v7.2 through the existing source-backed
production chain at execution time and writes only the fixed approved-profile
sidecar, never the chat,
memory, knowledge, review, or runtime databases. No model is called and nothing
is sent to a model. See `docs/approved_profile_control_v8_0_a_lite.md`.

## v8.0-B/C DeepSeek Runtime Thin Slice

v8.0-B/C adds one single-provider, single-turn, non-streaming execution path.
At execution time the fixed production wrapper rebuilds the source-backed v7.4
delivery and validates the v8.0-A active approved-profile projection. It then
creates a deterministic artifact binding the delivery identity, final
`model_context`, approved-profile authority, system instruction version, fixed
`deepseek-chat` configuration, and exact payload hash.

The DeepSeek adapter is transport-only: it sends the request, parses the answer
and token usage, and returns sanitized provider errors. It cannot access a
database, retrieve memory, approve candidates, or modify a profile. The API key
comes only from the documented environment credential or ignored
`config_private/deepseek.env`; it is never persisted or returned.

Runs are appended only to an independently initialized runtime sidecar. Its
event-derived state machine is `prepared -> running -> succeeded|failed`.
Unique request/mutation IDs replay the first immutable terminal receipt, and an
interrupted running attempt must be explicitly failed closed before a new-ID
retry. No response is automatically converted into long-term memory.

```powershell
python tools/runtime_thin_slice.py preview --question "What do I prefer?"
python tools/runtime_thin_slice.py sidecar-status
python tools/init_runtime_sidecar.py
```

Preview and initializer dry-run create nothing and make no external call.
Production initialization and execution each have separate explicit
confirmation gates; normal startup does neither. The Web API exposes fixed
real-readonly preview/status routes, while execute/recovery require explicit
local `run.ps1 runtime-admin`. Tests use only temporary runtime sidecars and
mock providers. See `docs/deepseek_runtime_thin_slice_v8_0_bc.md`.

## v8.0-G Production Runtime Execution UI

v8.0-G closes the production UI chain on the fixed D1 protocol. The production
Web routes no longer import or call the legacy B/C runtime. A preview rebuilds
the shared source-backed v8.0-E Delivery, validates the active profile
projection, and creates the immutable D1 request artifact for
`deepseek-v4-flash`, `stream=false`, and `thinking=disabled`.

The Runtime Execution page displays the exact question, complete model context,
4/4 approved-profile projection, evidence anchors, diagnostic
Delivery/Assembly/D1 artifact identities, and the separate
`v8.0-g-reviewed-execution-binding-1` authority. The reviewed identity seals
ordered profile/evidence IDs and anchors, context/wire hashes, fixed provider
configuration, request/mutation IDs, and its idempotency fingerprint while
excluding process-local capability diagnostics. Send remains disabled until
the user explicitly confirms that frozen reviewed binding. Execution rebuilds
and compares it, then validates the read-only D1 baseline and idempotency state,
before constructing the fixed adapter or permitting any Runtime write,
credential read, HTTP request, or network access. The append-only D1 ledger
records one attempt with no automatic retry and returns the response, actual
usage, and execution phase.

```text
GET  /api/runtime-preview?q=...&budget=Medium&request_id=...&mutation_id=...
GET  /api/runtime-run?model_run_id=...
POST /api/runtime-execute    # Runtime-admin only; exact bindings + confirmed=true
```

`web-real-readonly` exposes preview only. `run.ps1 runtime-admin` is the fixed
production execution surface. Visible development acceptance uses an already
initialized temporary D1 sidecar:

```powershell
.\run.ps1 runtime-admin-mock E:\path\outside-repo\runtime-ui-mock.db
```

Mock mode refuses the production Runtime sidecar, reads no credential, and
makes no provider request. See
`docs/production_runtime_execution_ui_v8_0_g.md`.

## v8.0-H Review-only Runtime Preview

`runtime-review` is the explicit Gate F1 review surface. It uses the same fixed
production Delivery, D1 artifact, and v8.0-G Reviewed Binding builders, while
loading no production execution facade, process-local authority, provider
Adapter, credential loader, ledger writer, HTTP request, or network path.
The initialized production Runtime is inspected only through the immutable
readonly status reader.

```powershell
.\run.ps1 runtime-review
```

The page requires caller-specified `request_id` and `mutation_id` fields and
preserves their exact bytes across repeated builds. It displays the complete
reviewed object, diagnostic and reviewed identities, ordered profile/evidence
IDs and anchors, uncertainty, final model context, fixed DeepSeek v4 Flash
configuration, estimated input tokens, and all zero-execution flags. The same
confirmation area is present, but its checkbox remains unchecked and disabled,
and its Send button remains disabled with an explicit authority-absent notice.

The server reports `runtime_review=true`, `runtime_admin=false`,
`readonly=true`, `execution_authority_present=false`, and `can_execute=false`.
Every review-only POST to `/api/runtime-execute` is rejected with HTTP 403
before the request body is read. `runtime-admin` remains a separate explicit
launch mode with its existing server-side Reviewed Binding validation.
See `docs/production_runtime_review_v8_0_h.md`.

## Database Policy

The repository does not provide a real database.

Do not commit:

- `data/ayunos_chatgpt.db`
- `data/*.db`
- `data/*.sqlite`
- `backups/*.db`
- raw DeepSeek or ChatGPT exports
- markdown, JSON, or TXT files containing real chat records

Allowed public structure includes `docs/schema.sql`, source code, tests, mock demo materials, `.env.example`, and `.gitkeep` placeholders.

## Create A Test Database

The test database is synthetic and safe for development. Create or refresh it with:

```powershell
.\run.ps1 create-test-db --overwrite
```

The generated file lives at:

```text
data/test/ayunos_memory_test.db
```

It is ignored by Git. Recreate it locally whenever needed instead of committing it.

## Start The Web UI

Start the Web UI against the sanitized test database:

```powershell
.\run.ps1 web
```

Then open:

```text
http://127.0.0.1:8765
```

For real local data inspection, use:

```powershell
.\run.ps1 web-real-readonly
```

Do not use real-mode commands for public demos or repository screenshots unless the data has been separately sanitized.

## Configuration

Use `.env.example` as a placeholder template only. v2 local tools do not require OpenAI, DeepSeek, Claude, or Anthropic keys.

Do not commit a real `.env` file.

## Safe Git Workflow

Before staging:

```powershell
.\run.ps1 smoke
.\run.ps1 scan
.\run.ps1 audit
git status
```

Review `git status` manually. The status output must not include real databases, raw exports, backups, logs, or real chat records.

## v5.1 Memory Change Detection

YunOS v5.1 provides a read-only source-change detector for memory coverage:

```powershell
python tools/check_memory_changes.py --json
```

It reads messages through SQLite `mode=ro`, emits only source identifiers and
classification metadata, and never generates summaries or embeddings. Current
real acceptance is `summary_missing=177` and `coverage_unknown=1`. See
`docs/memory_change_detection_v5_1.md` for the sealed boundary and v5.2 handoff.

## Public Demo

`public_demo/` contains mock-only product briefs, brand notes, website prompts, and sample memory items. It is intended for public-facing design exploration and should not be mixed with private records.
