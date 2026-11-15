# YunOS v6.0 Evidence-backed Memory Graph Foundation

## Scope

v6.0 establishes a deterministic and auditable graph foundation over existing
summary evidence. It is a normalization and candidate-preview layer, not a
fact-extraction system. It never confirms a relationship, writes a production
graph, initializes a production sidecar, generates embeddings, calls an
external API/LLM, or starts background work.

The graph recognizes only these node types:

- `conversation`
- `summary`
- `memory_space`

The graph recognizes only these relationships:

- `belongs_to_conversation`: summary to conversation, directed and
  deterministic.
- `belongs_to_memory_space`: summary to memory space, directed and
  deterministic.
- `newer_version_of`: a newer accepted sidecar summary to the immediately
  preceding accepted version in the same conversation. Version gaps and
  cross-conversation pairs never produce this edge.
- `semantic_related`: an unordered summary pair backed by compatible stored
  embeddings and a cosine score at or above the configured threshold. It is a
  candidate only.

Relations such as contradiction, support, cause, inferred identity, personal
relationship, or free-form fact links are outside this phase.

## Canonical summary source

Graph discovery uses `memory.summary_repository.graph_summary_sources`; it
does not introduce a second summary reader.

1. Every accepted `summary_versions` record is retained so version history can
   be represented.
2. For a conversation with accepted versions, legacy `memory_summaries` is
   hidden by default.
3. For a conversation without an accepted version, the latest legacy
   conversation summary is the fallback.
4. `--include-legacy` includes a superseded legacy identity alongside accepted
   versions for review.
5. Accepted rows use their stored `source_snapshot_hash`; legacy rows bind the
   canonical read-only source snapshot produced by `memory.source_snapshot`.

Source text is used only in memory to verify `summary_text_hash`. It is never
placed in a node, edge, unavailable record, CLI result, file, or log.

## Node identity

Summary node identity hashes a canonical JSON envelope containing:

- `node_schema_version`
- `node_type`
- `source_table` and `source_id`
- `conversation_id`
- `summary_version`
- `summary_text_hash`
- `source_snapshot_hash`
- `memory_space`

`source_table` prevents a legacy numeric ID from colliding with the same value
in `summary_versions`. A version, summary hash, source snapshot, or memory
space change produces a new summary node ID. Rebuilding the same source
produces the same ID.

Conversation identity is based on the stable conversation key, rather than a
mutable scope label. Memory-space identity is based on the exact scope name.
The conversation node therefore always stores `memory_space = null`; summary
nodes and `belongs_to_memory_space` edges carry scope membership. The same
conversation has one identical canonical payload across filters and builds,
even when its summaries belong to different spaces.
All public node records have a common safe metadata shape; text, absolute
paths, embeddings, and prompts have no columns.

## Edge identity and evidence

Every edge binds:

- `edge_schema_version`
- relation type, endpoints, and direction
- generator identity
- generator configuration hash
- evidence identity
- score and threshold when semantic
- status

Undirected endpoints are lexically normalized before hashing, so a reversed
pair cannot create a second semantic edge. Changing a node, evidence record,
embedding vector hash, score, threshold, or generator configuration changes
the edge ID.

Supported statuses are `deterministic`, `candidate`, `unavailable`, and
`manual_review`. v6.0 emits actual ownership/version edges as deterministic and
qualified semantic edges as candidates. Missing embeddings, stale content
hashes, legacy/unversioned providers, malformed vectors, incompatible provider
identities, version gaps, and below-threshold scores are safe `unavailable`
diagnostics. `manual_review` is reserved by the schema for a later controlled
review workflow; v6.0 does not set it automatically.

## Semantic candidate rule

The preview reads existing `memory_embeddings` metadata and vectors in SQLite
read-only mode. It does not instantiate an embedding provider or load/download
a model.

For each summary, an eligible embedding must match `source_table + source_id`,
`conversation_id`, `summary_version`, `summary_text_hash`,
`source_snapshot_hash`, and `memory_space`. Its metadata must also identify the
same summary source, summary ID, conversation-summary kind, and
`summary_embedding` target. Its provider, model, revision, fingerprint,
protocol, normalization flag, dimension, and vector shape must be complete.
Missing, empty, malformed, mistyped, or mismatched metadata is unavailable;
v6.0 never infers missing identity from the latest summary or from a numeric
source ID. Legacy records without complete evidence remain safely unavailable.
The evidence identity hashes normalized verified metadata, provider identity,
content hash, and vector hash; SQLite row order and generated timestamps are
not evidence identity inputs.
Compatible current records take precedence over any `legacy_unversioned`
record. Two summary nodes are compared only when they share a complete
embedding identity. If more than one identity is shared, the documented
lexical identity rule selects one deterministically. The default cosine
threshold is `0.80`.

Memory-space membership alone never creates `semantic_related`.

## Knowledge sidecar boundary

The fixed future production database is
`data/knowledge/ayunos_knowledge.db`. The future backup location is
`backups/knowledge/`. Both are constants only in v6.0; their directories and
database files are intentionally not created.

The schema contains append-only `graph_nodes`, `graph_edges`, and
`graph_metadata` tables. Primary keys enforce identity uniqueness, foreign
keys bind edge endpoints, and triggers reject update/delete operations.

`initialize_test_knowledge_sidecar` and `TestKnowledgeGraphRepository` accept
only new database files below the operating system temporary directory. The
shared path policy rejects production targets, hardlinks/same-file aliases,
symlinks, junctions, reparse components, and paths that resolve outside the
temporary root. The repository appends complete graph builds, not arbitrary
production records. Every SQLite connection is explicitly closed.

The only production operation is `production_knowledge_status`, which opens an
existing fixed file with SQLite `mode=ro`. If the file is absent, it reports
`absent` without creating the directory or file.

Production initialization and controlled writes remain deferred to v6.2.

## Read-only preview

```powershell
python tools/preview_memory_graph.py --limit 5 --json
```

Supported options:

- `--json`
- `--limit` (clamped to `1..20`)
- `--conversation-id`
- `--memory-space`
- `--relation-type`
- `--min-score`
- `--include-legacy`

There is no `--execute`, `--write`, or `--accept` option. The command writes no
database, sidecar, file, backup, or log. It returns schema/generation metadata,
full counts, bounded safe node/edge/unavailable samples, and read-only
knowledge-sidecar status. It does not return summary text, message text,
content previews, embedding arrays, prompts, absolute paths, or exception
stacks.

## Verification contract

The v6.0 test set covers:

- legacy/sidecar source-ID collision resistance and stable node identities;
- identity changes for summary version, text hash, and snapshot changes;
- stable conversation and memory-space nodes;
- deterministic ownership edges and consecutive-version rules;
- undirected semantic deduplication, threshold rejection, unavailable
  evidence, and generator-config identity changes;
- current-compatible embedding precedence over legacy records;
- sidecar-over-legacy discovery and legacy snapshot binding;
- CLI filtering, limit clamping, forbidden write flags, and safe output;
- system-temporary repository enforcement, real hardlink/same-file and mocked
  reparse rejection, append-only triggers, rollback, and immediate Windows file
  release;
- unchanged production hashes/manifests and absent production knowledge
  initialization in the final seal checks.

All tests are local and offline. Synthetic vectors or the deterministic test
provider are used for semantic relationship tests; no model download or
network call is required.

## v6.1 handoff

v6.1 should design a human review envelope over immutable node, edge,
generator-config, and evidence identities. It should define stale-review
rejection, candidate supersession, explicit accept/reject decisions, and audit
redaction while remaining preview-only. Production schema initialization and
controlled append remain a separate v6.2 gate.
