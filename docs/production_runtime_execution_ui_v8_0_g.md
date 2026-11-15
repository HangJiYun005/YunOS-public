# YunOS v8.0-G Production Runtime Execution UI Closure

## Scope

v8.0-G connects the reviewed v8.0-E Production Delivery to the fixed D1
artifact, visible confirmation, D1 execution service, append-only Runtime
ledger, and visible response/usage result. It does not initialize or migrate a
production sidecar and it does not authorize a provider call by itself.

The production service fixes all of these dependencies:

- source-backed v8.0-E Production Delivery;
- active model-visible profile projection;
- `data/runtime/ayunos_runtime.db`;
- D1 schema, runtime protocol, artifact protocol, and renderer;
- provider `deepseek`, model `deepseek-v4-flash`;
- base URL `https://api.deepseek.com`;
- `stream=false`, `thinking.type=disabled`;
- strict ignored private-file credential loader;
- D1 canonical identity, artifact, event, receipt, and ledger validators.

No production entry accepts a caller path, connection, provider, model,
adapter, renderer, schema, artifact builder, delivery builder, validator,
resolver, or callback.

## Determinism investigation

Long-lived and fresh processes can observe different semantic/graph dependency
availability while selecting the same ordered profile/evidence items and
producing byte-identical model context and wire body. Those process-local
diagnostics correctly change the v8.0-E Delivery, Assembly, and D1 artifact
identities, because the released identities seal the complete diagnostic
objects. They must not, however, invalidate a user's approval when the actual
send semantics are unchanged.

`v8.0-g-reviewed-execution-binding-1` therefore seals only the reviewed send
semantics. Delivery, Assembly, and D1 artifact identities remain visible under
a separate versioned diagnostics identity. The released v7.4 and v8.0-E
identity algorithms are unchanged, and no current profile, evidence, or
message identity is hard-coded. Twenty fresh Python processes with the same
question, budget, IDs, and databases produce one unique reviewed binding.

## Preview and confirmation binding

`GET /api/runtime-preview` requires the exact question, budget, request ID, and
mutation ID. It returns the complete reviewed Delivery plus a public D1
artifact. The raw wire body is not returned; its SHA-256 is displayed.

The reviewed confirmation envelope binds:

1. exact question UTF-8 bytes/hash;
2. versioned budget profile and limits;
3. approved-profile projection identity/revision and ordered item IDs;
4. ordered evidence IDs and exact source anchors;
5. exact model-context bytes/hash;
6. fixed provider, model, base URL, stream, and thinking configuration;
7. artifact/renderer/system-instruction protocols and exact wire bytes/hash;
8. exact request ID and mutation ID;
9. a versioned reviewed artifact identity and idempotency fingerprint.

Capability availability, dependency failure reasons, import/cache state, and
other diagnostics that do not change the final send content are excluded from
this authority. They remain visible as diagnostic Delivery, Assembly, and D1
artifact identities and as `v8.0-g-runtime-preview-diagnostics-1`.

The execute endpoint rebuilds the Delivery and artifact and recomputes every
binding. Missing confirmation, an extra field, a changed question/budget/ID, or
any mismatched reviewed field fails before constructing the provider adapter,
before opening the Runtime sidecar for write, and before credential, HTTP
request, or network access. A read-only D1 baseline and idempotency check also
complete before the private fixed adapter factory is called. Production APIs
accept no adapter-factory or transport override.

## D1 ledger and outcomes

A confirmed run appends canonical D1 identities and events only:

```text
state_prepared
state_running
send_attempt_started
provider_response_received
state_succeeded | state_failed
```

Success appends one user message, one assistant message, usage, one receipt,
and the response. Provider failure appends a sanitized error and immutable
failure receipt. A persistence interruption after the send boundary remains
visible as an in-doubt non-terminal run; the production Web route exposes no
automatic recovery or resend fallback. Each request invokes the adapter at most
once and reports `automatic_retry=false`.

The D1 canonical ledger validator rechecks all conversation, artifact, run,
message, event, error, and receipt identities plus cross-table bindings before
the service uses an existing sidecar or returns a stored result.

## Credential boundary

The production v4 adapter reads only the fixed ignored file
`config_private/deepseek.env`. The accepted bytes are exactly one non-empty
`DEEPSEEK_API_KEY=<value>` assignment with at most one trailing line ending.
There is no process-environment or alternate-file fallback. The credential and
Authorization header are never returned, persisted, logged, or rendered.

## Web modes

- `web-real-readonly`: Runtime preview and status only; POST remains forbidden.
- `runtime-admin`: fixed production service with explicit startup confirmations
  plus the per-request visible confirmation gate.
- `runtime-admin-mock`: real source database remains immutable/read-only; only
  an already initialized non-production D1 sidecar is writable. The mock
  adapter reads no credential and makes no network request.

The mock page labels its mode, distinguishes diagnostic identities from
`授权发送身份 / Reviewed execution binding`, and reports
`network_request_count=0`, `credential_read=false`, exactly one simulated
adapter construction/call, response text, usage, and the full D1 event result.

## Focused verification

`tests/test_production_runtime_execution.py` covers preview completeness,
diagnostic-only drift, reviewed-content drift, success/failure ledgers, D1
canonical validation, idempotent replay/conflict, delayed adapter construction,
zero credential/HTTP/network access for rejected preflight, exact
single-call/no-retry evidence, strict credential format, B/C call-chain
isolation, API gating, and UI contract.
Existing D1 status/ledger tests continue to verify malformed identities,
cross-table failures, append-only protections, and zero-run production status.
