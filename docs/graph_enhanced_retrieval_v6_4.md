# YunOS v6.4 Graph-Enhanced Retrieval

## Unified read-only behavior

v6.4 adds one retrieval contract over message keyword matching, compatible
summary embeddings, and the sealed v6.3 knowledge query. The core entry is
`GraphEnhancedRetrievalService.search(RetrievalQuery(...))`; the CLI is
`tools/graph_enhanced_search.py`, and the local GET endpoint is
`/api/graph-enhanced-retrieval`.

No request parameter can select a database path. Production chat, semantic,
and knowledge files use their fixed repository paths and are opened read-only.
The implementation has no initialization or persistence fallback.

The sealed correction protocol is `v6.4-retrieval-2`.

## Time and anchor semantics

Omitted `start_time` and `end_time` mean all time. A date-only start means
00:00:00 UTC; a date-only end means 23:59:59.999999 UTC. ISO timestamps are
normalized to UTC. Both boundaries are inclusive.

The time predicate is applied to every keyword message and every message used
as a semantic or graph anchor. A related conversation with no message inside
the interval is not returned, so graph traversal cannot leak out-of-range
content.

The current semantic sidecar contains whole-conversation summary embeddings
without a message-time coverage binding. If either time boundary is present,
that semantic path therefore fails closed with
`semantic_time_evidence_unprovable`; it is not called and cannot become a graph
seed. All-time queries continue to use compatible summary embeddings. A future
message/time-bound semantic index may replace this restriction under a new
protocol, but v6.4 does not create or rebuild such an index.

Every result contains `conversation_id`, `anchor_message_id`, `time`,
`hit_types`, evidence-specific reasons, `sorting_basis`, and a stable
`ayunos://conversation/<id>?message=<id>` reference. `snippets` mode returns
only matching fragments by default and can add a bounded context window when
explicitly requested. `conversation_anchor` mode merges at
conversation scope and returns the stable message anchor needed to open and
highlight the full conversation.

Every evidence item also carries its own immutable `source` object containing
conversation ID, message anchor, time, role, title, and bounded snippet. Merging
never rewrites these sources. The public result anchor is rebuilt from the
highest-ranked evidence, so a keyword-primary result always keeps its actual
keyword source while semantic and graph evidence retain independent anchors.

## Seeds, graph rules, and budgets

Only keyword or semantic results are seeds. Requesting graph without either
direct channel fails request normalization with `graph_requires_direct_channel`.
When at least one requested and available direct channel executes but produces
no direct evidence, graph is not invoked and reports `reason_code=no_direct_seed`.
If no requested direct channel is executable, the reason is
`direct_channel_unavailable`. Graph traversal:

- defaults to one hop and allows at most two;
- consumes only v6.3 `formal` and `deterministic` relations;
- always sends `include_candidates=False`;
- rejects missing path relations and never traverses candidate or unavailable
  evidence;
- records the seed conversation/anchor, the complete canonical direct hit-type
  set at that exact anchor, the matching path start, path/depth, relation
  class/type, and evidence identity for each graph result;
- requires every graph seed to match direct evidence in the same retrieval
  envelope; a graph result can never seed another graph result;
- is bounded by relation, path, result, and context-character budgets.

The context-character budget includes the clipping marker. Values `0`, `1`,
`2`, and `3` therefore return at most `""`, `"."`, `".."`, and `"..."`
respectively; the aggregate context content never exceeds the declared limit.

Direct keyword results rank before direct semantic results, and all direct
results rank before graph-only results. Graph evidence can supplement a direct
hit but cannot move a weak graph-only hit into a direct tier. Ties use score,
descending timestamp, conversation ID, and message ID. Snippet mode limits one
conversation to three rows; conversation-anchor mode naturally returns one row
per conversation. Duplicate evidence is merged deterministically.

## Availability and fail-closed behavior

Semantic and knowledge availability are reported independently. Missing or
invalid sidecars produce `unavailable` reason codes and no fabricated hit.
Keyword results remain usable when they can be proven from the read-only source
database. A missing/unreadable source database makes the whole query
unavailable. Invalid time ranges, hit types, modes, cursors, and budgets are
rejected before retrieval.

Semantic failure codes are intentionally separate:

- `semantic_sidecar_missing`: the fixed sidecar file is absent;
- `semantic_sidecar_corrupt`: SQLite cannot read a valid database;
- `semantic_sidecar_invalid`: schema, record, vector, or identity validation
  failed;
- `semantic_dependency_unavailable`: the local runtime dependency is absent;
- `semantic_model_unavailable`: the configured local model/cache cannot load;
- `semantic_time_evidence_unprovable`: a bounded query cannot prove that a
  whole-conversation summary is inside the requested window.

## Examples

```powershell
python tools/graph_enhanced_search.py "神秘访客" --result-mode conversation_anchor --json
python tools/graph_enhanced_search.py "火星" --start-time 2025-01-01 --end-time 2025-12-31 --graph-depth 2 --json
```

```text
GET /api/graph-enhanced-retrieval?q=火星&result_mode=snippets&graph_depth=1
```
