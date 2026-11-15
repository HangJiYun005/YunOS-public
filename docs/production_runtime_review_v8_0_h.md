# YunOS v8.0-H Review-only Runtime Preview

## Purpose

Gate F1 needs a visible review surface, not an execution-capable process.
`runtime-admin` cannot satisfy that boundary because its explicit startup
creates the process-local `ProductionRuntimeExecutionSession` required by the
fixed production execute facade. The earlier real-readonly page also omitted
the confirmation checkbox and Send button, so it could not visibly demonstrate
their unchecked and disabled states.

v8.0-H adds a separate `runtime-review` launch mode. It does not weaken,
replace, or auto-enter `runtime-admin`.

## Process boundary

The review process imports `memory.production_runtime_review`, which exposes
only `ProductionRuntimeReviewService.preview`. The module uses the released:

- fixed v8.0-E production Delivery builder;
- D1 artifact builder and validator;
- v8.0-G Reviewed Execution Binding builder;
- immutable production Runtime status inspector.

It has no execute or run-retrieval method and imports none of these surfaces:

- production Runtime execution facade;
- `ProductionRuntimeExecutionSession` or its issuer;
- DeepSeek v4 Flash provider Adapter;
- credential loader or ignored private credential path;
- Runtime ledger writer;
- HTTP request or network transport.

`tools/local_web.py` imports the production execution module only inside the
two explicit admin startup branches. Review startup and preview therefore do
not load that module or the v4 transport module.

## Server gate

The review status projection is exact:

```text
runtime_review=true
runtime_admin=false
readonly=true
execution_authority_present=false
can_execute=false
```

When `runtime-review` receives `POST /api/runtime-execute`, the handler returns
HTTP 403 before reading or decoding the request body. A second gate in the
route method returns the same refusal for direct in-process dispatch. The
response reason is `runtime_review_execution_forbidden`.

These server gates remain authoritative if browser code, a checkbox, or a
disabled button is modified in the DOM.

## Exact review inputs and output

The page provides visible question, budget, `request_id`, and `mutation_id`
fields. In review mode all four values are submitted exactly. Missing IDs are
rejected; supplied IDs are not replaced, regenerated, trimmed, normalized, or
given timestamp/random fallbacks. Repeated Build actions therefore reproduce
the same reviewed identity for the same immutable production inputs.

The visible review includes:

- exact question and budget;
- complete immutable Reviewed Execution Binding JSON;
- Reviewed Artifact and D1 request artifact identities;
- diagnostic Delivery and Assembly identities;
- ordered profile IDs and 4/4 profile contents;
- ordered evidence IDs, eight evidence items, and source anchors;
- uncertainty section and complete final model context;
- context and wire SHA-256 values;
- DeepSeek v4 Flash, `thinking=disabled`, and `stream=false`;
- estimated input tokens explicitly labeled as estimated;
- `persisted=false`, `sent_to_model=false`, `external_model_calls=false`, and
  `credential_read=false`.

The same confirmation panel used by Runtime-admin is rendered in review mode.
Its checkbox is created unchecked and disabled. Its Send button is created
disabled. The panel states:

> Review-only mode: execution authority is absent. Sending is unavailable.

## Compatibility

The v8.0-G Reviewed Binding, Reviewed Artifact, Delivery, Assembly, D1 artifact,
model-context, and wire algorithms are unchanged. A focused regression compares
review-only preview output byte-for-byte with the released production execution
preview for the same fixed Delivery fixture.

`runtime-admin` keeps its process-local authority, server-side binding rebuild,
explicit per-request confirmation, append-only ledger path, and fixed Adapter.
`runtime-admin-mock` retains its one-confirmed-mock-turn acceptance path.
`web-real-readonly` remains preview-only and rejects POST.
