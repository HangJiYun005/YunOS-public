# YunOS v8.0-E Production Delivery Integration

v8.0-E adds one versioned, fixed-path Production Delivery builder for Gate C,
the real-readonly Web UI, the D1 runtime preview, and the earlier B/C preview.
It composes the sealed v7.4 Delivery and does not change v7.4 protocol or
identity semantics.

## Production identities

- Delivery protocol: `v8.0-e-production-delivery-1`
- Delivery identity: `pdv_<sha256>`
- Assembly protocol: `v8.0-e-production-context-assembly-1`
- Assembly identity: `pca_<sha256>`
- approved-profile policy: `v8.0-e-active-model-visible-all-1`
- lexical fallback: `v8.0-e-readonly-fts-fallback-1`

The implementation is `memory/production_delivery.py`. Production callers do
not accept source database paths, profile projections, providers, credentials,
or prebuilt artifacts.

## Approved-profile inclusion

The v8.0-E policy includes every item in the current D1 active projection that
is both approved by the user and `model_visible=true`. It performs no implicit
task-relevance pruning. Rejected, pending, non-visible, duplicate, or malformed
items cannot enter the Delivery. The profile binding is derived from the
Delivery's actual approved layer, and runtime artifact validation compares the
same ordered identity list.

This policy is intentionally limited to v8.0. A future scope/relevance policy
belongs to v8.1 and must use a new versioned contract.

## Readonly lexical / FTS degradation path

When semantic or graph retrieval is unavailable, the builder reads the fixed
production main database with SQLite `mode=ro&immutable=1`. It combines FTS5
matches with literal lexical matches so compound forms such as product names
without spaces are not lost. It then:

1. prioritizes direct user messages;
2. expands only bounded, query-bound neighboring user messages;
3. preserves high-information query terms;
4. deduplicates normalized text;
5. selects a diverse set across use, experience, display constraints,
   mobility/battery, configuration, price thresholds, purchase plans, and
   conditional or changing views;
6. marks volatile prices or promotions `potentially_stale`;
7. retains a composite `(conversation_id, message_id)` anchor for every item.

The Delivery explicitly remains degraded when this fallback is active. It does
not claim that historical messages are durable profile facts, current market
facts, or a final product decision.

No message IDs, Gate C diagnostic results, or E01-E12 golden anchors are
hardcoded in production logic.

## Fixed surfaces

- CLI: `python tools/context_delivery_preview.py <question> --json`
- Web: `GET /api/context-delivery-preview` in real-readonly mode
- B/C preview: `RuntimeThinSliceService().preview(...)`
- D1 preview: `create_production_v4_flash_runtime().preview(...)`

For the same question, budget, production profile revision, and immutable main
database, all four surfaces produce the same `pdv_...` Delivery identity.

## Safety boundary

Every preview reports:

- `readonly=true`
- `persisted=false`
- `sent_to_model=false`
- `external_model_calls=false`
- `credential_read=false`

v8.0-E does not initialize the runtime sidecar, read a DeepSeek credential,
contact a provider, create a proposal/decision, or modify profile, main,
semantic, memory, knowledge, or review data.

## Validation

Focused tests:

```powershell
python -m unittest tests.test_production_delivery `
  tests.test_context_delivery `
  tests.test_runtime_enablement `
  tests.test_runtime_thin_slice
```

Real-readonly acceptance uses the Gate C question from
`docs/production_enablement_first_turn_v8_0_d.md`, verifies 4/4 approved items,
source-backed historical evidence, exact profile/evidence artifact bindings,
the full visible model context, and source-detail navigation in the browser.
