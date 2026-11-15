# YunOS v7.1 Evidence Selection and Budgeting

## Protocol and boundary

The canonical review-hardened protocol is `v7.1-evidence-selection-2` with
selection policy `v7.1-selection-policy-2`. The earlier untagged `-1` schema
is not silently reinterpreted. The `-2` protocol consumes a fully validated, immutable
`v7-memory-package-1`; v7.0 fields, identities, validators, and public entry
points are unchanged. The v7.1 layer is read-only and performs no model call,
summary, rewrite, classification, conflict resolution, persistence, sidecar
initialization, or long-term-memory update.

The core is `memory/evidence_selection.py`. Public previews are fixed to the
production source path and accept no database path:

```powershell
python tools/evidence_selection_preview.py "search terms" --budget small --json
python tools/evidence_selection_preview.py "search terms" --budget custom `
  --character-budget 8000 --estimated-token-budget 2000 `
  --conversation-cap 4 --max-selected 20
```

```text
GET /api/evidence-selection-preview?q=search%20terms&budget=medium&format=json
GET /api/evidence-selection-preview?q=search%20terms&budget=medium&format=text
```

The GET endpoint is available only when the Web process uses the exact
production main database with both real and readonly mode. Test mode, writable
real mode, or any other path returns `production_readonly_required`.

## Structural deduplication

Deduplication is deterministic and inference-free:

1. Evidence with the exact `(conversation_id, message_id)` source anchor is
   merged into one candidate. Every keyword, semantic, and graph hit remains
   in canonical `hit_provenance`; the excerpt is rendered once.
2. Anchor candidates are exactly equivalent only when their roles match and
   their excerpts match after Unicode NFC plus CRLF/CR-to-LF normalization.
   The representative keeps `duplicate_sources`, including every duplicate
   source anchor, evidence identity, and hit provenance. No fuzzy match,
   semantic similarity, or model rewrite is allowed.
3. Each excluded duplicate also has an omitted manifest with `reason=duplicate`
   and no excerpt/body.

Candidate, v7 evidence, source anchor, time, reference URI, and graph seed/path
details remain bound in the selected or omitted provenance records and in the
canonical candidate ledger.

## Canonical candidate ledger and source projection

`candidate_ledger` contains every merged anchor exactly once and contains no
excerpt body. Each entry binds the full source anchor and displayable title,
role/time, evidence identities and complete provenance, derived direct/graph
tier, duplicate equivalence key and canonical representative, and deterministic
render cost. Its excerpt commitment records SHA-256 for the canonical redacted
text, Unicode-code-point length, and the corresponding NFC/newline-normalized
hash and length. A selected body must reproduce both commitments and every
underlying v7.0 evidence identity. Duplicate and omitted bodies are never added
to the ledger.

`source_projection` carries the declared v7.0 `mp_` identity plus canonical
request, retrieval availability/envelope, capability/degradation/warning state,
and ordered evidence identities. Standalone validation reuses the v7.0 request,
retrieval, capability, degradation, warning, evidence-detail, graph-seed, and
absolute-path validators. It derives the public v7.1 request, capabilities,
degradation, warnings, missing capabilities, status, and reason from this
projection; callers cannot change those public fields independently without
rebuilding the projection and every dependent identity. Only source-backed
validation establishes that the declared projection is the real source.

## Selection policy and budgets

Built-in profiles are:

| Profile | Character budget | Estimated token budget | Per-conversation cap | Max selected |
| --- | ---: | ---: | ---: | ---: |
| Small | 2,000 | 500 | 2 | 6 |
| Medium | 6,000 | 1,500 | 3 | 16 |
| Large | 16,000 | 4,000 | 5 | 40 |

`Custom` is controlled to 256-100,000 characters, 64-25,000 estimated tokens,
1-20 candidates per conversation, and 1-100 selected candidates. Named
profiles reject overrides.

The character budget applies to `model_payload` and is an absolute hard upper
bound: `used_characters == len(model_payload)`. The deterministic estimator is
`unicode-codepoint-ceil-div4-v1`, defined as
`ceil(Unicode code point count / 4)`. It is explicitly a planning estimate,
not the exact token count for any model. A v8 model adapter may rebudget the
selected evidence with its actual tokenizer.

Candidates are split into `direct` (keyword or semantic provenance) and
`graph_only` tiers. All direct candidates are considered first. Within each
tier, a stable conversation round-robin plus a per-conversation cap prevents a
high-frequency conversation from consuming the package. If a direct candidate
is excluded by the conversation cap, max-selected limit, or either budget,
the direct tier is incomplete and every graph-only candidate is excluded as
`lower_priority`; graph-only evidence therefore cannot occupy capacity after
any direct representative was not admitted.

Omission reasons are exactly `duplicate`, `conversation_cap`,
`budget_exceeded`, and `lower_priority`. Omitted records contain identities,
anchors, time, cost, priority, and hit/graph provenance but never the source
excerpt/body.

## Determinism and validation

`msp_` package identity is SHA-256 over canonical UTF-8 JSON with sorted keys,
no insignificant whitespace, and the `package_identity` field removed. It
binds the v7.0 package identity, normalized request and time, capability and
degradation states, versioned policy, selected/omitted manifests, counts,
budgets, estimator profile, and exact model payload. The same database state,
request, policy, and budgets produce the same package identity.

Standalone validation proves internal consistency, not source authenticity. It
recomputes candidate ordering, conversation round-robin, caps, direct-before-
graph priority, selection, omission reasons, counts, payload, character/token
usage, privacy rules, and package identity. An incomplete mutation (for
example, changing selected, omitted, count, policy, or budget fields without
rebuilding every dependent field) fails closed because the derivations no
longer agree.

A fully coordinated repackage is different. A party may replace source text,
capability or availability state, source status, v7 evidence identities, the
v7 source package identity, candidate ledger, all derived manifests and the
final `msp_` identity. If the resulting object is internally canonical and
privacy-safe, standalone validation may accept it. This is intentional and is
the same unkeyed-identity boundary as v7.0: without a trusted external anchor,
signature, key, or database reconstruction, a self-contained validator cannot
distinguish two independently self-consistent packages.

Any non-duplicate direct candidate omitted by `conversation_cap`,
`max_selected` (reported as `lower_priority`), or `budget_exceeded` makes the
direct tier incomplete. Every graph-only candidate is then deterministically
omitted as `lower_priority`. Graph-only candidates may use remaining capacity
only when every direct representative was selected.

Selected fields, ledger display fields, source projection, warnings, omitted
metadata, model payload, and renderer input are recursively checked with the
v7.0 Windows/UNC/POSIX/file-URI absolute-path boundary. Validation errors never
echo the rejected path.

Source-backed validation first uses the v7.0 trusted-source validator against
the controlled read-only SQLite database, then rebuilds the complete v7.1
candidate ledger, selection plan, manifests, payload, budgets and identity and
compares them exactly. A coordinated standalone-valid repackage cannot pass
this reconstruction unless it matches the real source.

The trust model is therefore:

| Validator | Proves | Does not prove |
| --- | --- | --- |
| standalone | schema, canonical unkeyed identities, privacy, ledger/dedupe/order, selection, omissions, counts and budgets | database authenticity or that a coordinated source repackage is the original package |
| source-backed | the standalone properties plus exact v7.0/v7.1 reconstruction from controlled read-only SQLite | signatures, external attestations or persistence |

Production CLI and GET preview paths call the production builder, whose
`EvidenceSelectionService.build` performs source-backed validation before it
returns. JSON serialization and formal text rendering occur only after that
successful return. The generic canonical JSON and renderer helpers still
perform standalone validation when used outside a production entry point; they
do not independently claim source authenticity.

Source `ok`, `partial`, `empty`, and `unavailable` state, time range,
capabilities, degradation reasons, warnings, anchors, and graph seed details
are propagated. JSON contains `model_payload`; text preview deterministically
reports the same identity, policy, counts, budget, omission counts, and exact
payload.
