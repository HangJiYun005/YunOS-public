# YunOS v7.4 Transparent Context Review and Delivery

## Scope

v7.4 adds a review-and-delivery layer over the sealed v7.3 context assembly.
It does not retrieve, rank, deduplicate, interpret, approve, or promote memory
independently. Every delivery embeds one validated v7.3 assembly and binds its
`ctx_` identity.

The production path is local and read-only. It accepts no database path,
profile input, provider, output path, or persistence target. It creates no
sidecar, makes no external LLM/API call, and always reports:

```text
persisted=false
sent_to_model=false
external_model_calls=false
```

These flags describe a preview envelope, not a completed model request.

## Four review layers

The envelope exposes exactly four layers:

1. `current_task`: the current query, normalized time range, retrieval state,
   capability matrix, degradation, missing capabilities, and warnings.
2. `approved_profile`: independently supplied user-approved items only. The
   production input is currently empty, so the UI explicitly displays the
   empty state. Candidate promotion remains forbidden.
3. `candidate_memory`: v7.2 items retain `candidate/not_approved` state and may
   be used only as auxiliary references. Relations are removed automatically
   if a delivery exclusion removes either endpoint.
4. `historical_evidence`: v7.1 evidence remains historical evidence, with
   conversation/message anchors and citation-first presentation.

Each review item carries inclusion state, trust status, character count,
non-model token estimate, source anchor when available, and all omission
reasons projected by v7.3 or v7.4. Candidate, historical-evidence, relation, and
expanded-excerpt representations also declare canonical `source_ids`,
`evidence_ids`, and `event_ids` provenance as applicable.

## Temporary exclusions

`excluded_item_ids` may identify only whole approved-profile, candidate, or
historical-evidence items that were included in the original assembly. The
current task is not excludable. Unknown, malformed, or stale identities fail
closed.

Exclusions are canonicalized, identity-bound, and applied only while building
the current delivery. They do not update profile approval, temporal candidates,
historical messages, v7.1 selection, or SQLite. Re-including an item means
rebuilding without its identity in `excluded_item_ids`.

For candidate and historical-evidence exclusions, v7.4 constructs a typed
provenance graph from the sealed v7.3 projection. The requested item seeds a
deterministic connected closure over matching source/evidence/event identities.
All candidate/evidence representations in that closure are omitted, and every
relation with an excluded dependency or incomplete endpoint is omitted. Text is
never used as a closure key: equal text with disjoint provenance remains.

`controls.exclusion_closure` records requested IDs, resolved item and relation
IDs, excluded provenance, and per-item causes. `model_payload_provenance` records
the union used by the final structured layers and expanded excerpts. Validation
recomputes the closure, checks relation endpoints, checks expanded-excerpt
provenance, and requires the excluded and model-payload provenance sets to be
disjoint. Exact delivery reconstruction then binds those layers to the rendered
`model_context`.

Changing the query, time range, or budget creates a new v7.3 assembly first.
The Web UI clears exclusions belonging to the previous assembly before that
reassembly so stale item IDs cannot cross assembly boundaries.

## Budget and time behavior

Small, Medium, Large, and bounded Custom controls are forwarded unchanged to
v7.3. Time boundaries are forwarded through `RetrievalQuery` and normalized to
UTC by the existing retrieval chain. v7.4 never backfills an excluded item with
an independently selected replacement; it filters the already validated v7.3
result and recomputes complete layers, relations, anchors, counts, budget usage,
and final model context.

The estimator remains `unicode-codepoint-ceil-div4-v1`. It is deterministic but
is not an exact model tokenizer.

## Delivery identity

Protocol: `v7.4-transparent-context-delivery-1`

Identity: `dvy_` plus the SHA-256 digest of the canonical JSON envelope without
the `delivery_identity` field. The identity covers:

- the complete original v7.3 assembly and its `ctx_` identity;
- normalized query and time range;
- budget profile and all limits;
- canonical current-delivery exclusions;
- final four layers, omissions, source anchors, counts, and capability state;
- final `model_context`;
- the read-only, non-persisted, non-sent execution flags.

Standalone validation validates the embedded v7.3 assembly and rebuilds the
entire v7.4 projection. Trusted-assembly validation additionally requires exact
equality with the independently supplied v7.3 assembly. Canonical JSON and text
rendering display the same `dvy_` and `ctx_` identities and the same final
`model_context`.

## CLI

```powershell
python tools/context_delivery_preview.py "MacBook" --budget medium --json
python tools/context_delivery_preview.py "MacBook" --start-time 2025-01-01 --end-time 2025-12-31
python tools/context_delivery_preview.py "MacBook" --budget custom --total-characters 8000 --estimated-tokens 2000 --max-evidence 12 --max-candidates 6 --max-history-expansions 1
python tools/context_delivery_preview.py "MacBook" --exclude <item-id> --json
```

There is deliberately no `--db`, approved-profile, provider, save, send, or
model option.

## GET API and Web UI

The GET-only endpoint is:

```text
/api/context-delivery-preview?q=...&start_time=...&end_time=...&budget=medium&exclude=<item-id>&format=json|text
```

Custom budget parameters are `total_characters`, `estimated_tokens`,
`max_evidence`, `max_candidates`, and `max_history_expansions`. Repeated or
comma-separated `exclude` values are accepted and canonicalized.

The endpoint is available only when `tools/local_web.py` is running against the
fixed production database in real-readonly mode. All other modes return
`production_readonly_required` without opening a caller-supplied path.

The “上下文预览” page provides query/time controls, budget selection, four
collapsible layers, include/exclude checkboxes, source-message navigation,
omission reasons, capability degradation, identities, and the exact final model
context. UI adjustments remain in page memory for the current delivery only.

Source-message navigation calls:

```text
/api/message-context?conversation_id=...&message_id=...&window=3
```

Both identities are mandatory and the SQL predicate matches both. Zero rows,
more than one row, missing fields, and mismatched or forged combinations fail
closed. There is no fallback query by `message_id` alone.
