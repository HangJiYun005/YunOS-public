# YunOS v6.3 Knowledge Graph Query and Explanation

## Scope

v6.3 adds a bounded, read-only query layer over the v6.0-v6.2 knowledge
protocol. It does not initialize a sidecar, write a database or Review
Authority, infer a relation, accept a candidate, call a model, or contact the
network.

The fixed production source remains `data/knowledge/ayunos_knowledge.db` and is
opened with SQLite `mode=ro` plus `query_only=ON`. A missing sidecar returns
`status=unavailable` and `reason_code=knowledge_sidecar_not_initialized`
without creating a directory or file.

## Relation classes

Results never merge these classes:

- `formal`: only active rows in `accepted_relation_versions`. Each result
  carries `relation_version`, both node payload hashes, evidence digest,
  graph-build identity, and the candidate/plan/decision chain that v6.2
  persisted. Every active row must have exactly one matching persistence
  receipt. The query reconstructs the v6.1 candidate identity and the v6.2
  persistence, transaction, and receipt identities from the stored relation,
  complete store binding, authority mutation head, backup identity, and anchor
  assessment. Missing or mismatched receipts, stores, payload hashes, or
  identities fail closed before any formal result is returned.
- `deterministic`: only `graph_edges.status=deterministic`. Each result carries
  `edge_schema_version`, generator configuration, and evidence identity.
- `candidate`: `graph_edges.status=candidate` or the schema-reserved
  `manual_review` state. Candidates are omitted by default and appear only
  under `relations.candidates` when explicitly requested. They are never
  presented as formal facts.
- `unavailable`: stored unavailable edges are isolated under
  `relations.unavailable` and never traversed. Sidecar absence, unavailable
  schema, or an initialized store with no formal relations is also reported
  explicitly. Human decisions that were not persisted are not queried or
  promoted.

The query verifies the sealed schema and store identity, recomputes every node
identity, and checks formal endpoint payload hashes before returning results.
Any mismatch fails closed.

## Query surface

```powershell
python tools/query_knowledge_graph.py --conversation-id <id> --max-depth 2 --json
python tools/query_knowledge_graph.py --memory-space whale --relation-type semantic_related --include-candidates --json
```

The local API provides the same contract:

```text
GET /api/knowledge-graph-query?conversation_id=<id>&max_depth=2&limit=25
```

Supported filters are `conversation_id`, `memory_space`, `relation_type`, and
`node_id`. Explicit conversation, node, or memory-space filters that match no
node return an empty result; only a query without those seed filters uses the
global bounded seed set. `max_depth` is limited to `0..3`; `limit` defaults to
25 and is limited to `1..100`. There is no database-path, write, execute,
accept, authority, inference, or free-form relation option.

Traversal uses deterministic ordering, refuses to repeat a node within a path,
and stops at both the depth and result limits. `limit` is the combined budget
for traversable relations and paths, and separately bounds unavailable rows,
seed nodes, and related-memory aggregates. The `nodes` array is the endpoint
closure required to verify every returned relation and path, so it may exceed
`limit` while remaining bounded by those budgets and maximum depth. Related
memory aggregation returns summary identities, versions, hashes, memory
spaces, and path IDs only.

## Explanation and privacy

Every relation and path includes a fixed, redacted explanation with a
machine-readable `basis_code`. Explanations describe only the applicable
protocol: deterministic ownership/version binding, a non-formal semantic
candidate, or a persisted accepted relation. They do not regenerate or quote
source content.

Outputs exclude summary/message text, embeddings, raw metadata, timestamps,
absolute paths, prompts, exception stacks, and model-generated claims. A
deterministic `query_identity` binds the normalized query, sealed store
identity, and returned node/relation/path identities.
