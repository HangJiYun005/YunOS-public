# YunOS v8.0-B/C DeepSeek Runtime Thin Slice

## Scope

This phase adds one auditable, non-streaming, single-turn model-call path:

`question -> source-backed v7.4 Delivery -> deterministic request artifact -> DeepSeek adapter -> response -> runtime sidecar`

It does not change the sealed v7.0 through v8.0-A protocols. Multi-provider
routing, Ollama, streaming, multi-device synchronization, automatic approval,
automatic memory creation, PWA work, and a full chat UI remain out of scope.

Protocol: `v8.0-bc-deepseek-runtime-thin-slice-1`

Request artifact: `v8.0-bc-runtime-request-artifact-2`

Runtime sidecar: `v8.0-bc-runtime-sidecar-2`

## Trusted input and request identity

The production runtime rebuilds the v7.4 delivery at execution time from the
fixed read-only chat database. It also reads the validated v8.0-A active
approved-profile projection. Model-visible approved items are passed into the
existing v7.3/v7.4 builders through the v8 runtime wrapper; no sealed protocol
or identity rule is modified. A missing production profile sidecar produces the
existing explicit empty projection.

The request artifact deterministically binds:

- v7.4 delivery protocol, delivery identity, assembly identity, final
  `model_context` SHA-256, and character count;
- v8.0-A projection identity, revision, decision count, model visibility, and
  exact approved profile item identities;
- system instruction version and SHA-256;
- fixed provider `deepseek`, model `deepseek-chat`, temperature, output limit,
  and `stream=false`;
- the exact two-message provider payload, canonical HTTP body bytes, base64
  transport snapshot, and `wire_body_sha256`.

`artifact_identity` is the SHA-256 identity of that complete canonical object.
Preview creates neither a sidecar nor a model run and reports
`persisted=false`, `sent_to_model=false`, and `external_model_calls=false`.

## Adapter boundary

`memory/deepseek_adapter.py` owns only deterministic provider rendering, HTTPS
transport, response parsing, token usage, and sanitized provider errors. It imports no
SQLite, retrieval, profile, candidate, or sidecar module. It accepts only the
fixed DeepSeek model and an immutable prepared request derived from a complete,
identity-checked artifact. The HTTP layer sends the artifact's validated bytes
directly and never serializes a payload a second time.

The credential is read from the environment variable documented in
`.env.example` or ignored `config_private/deepseek.env`. It is never added to an artifact, database,
receipt, log payload, API response, browser field, or tracked file.

## Runtime sidecar schema

The independent runtime database contains:

| Table | Append-only content |
| --- | --- |
| `runtime_schema` | schema identity and initialization time |
| `runtime_conversations` | one deterministic conversation identity per run |
| `runtime_request_artifacts` | full delivery snapshot, request artifact, and all hashes/bindings |
| `runtime_model_runs` | request/mutation identities, fingerprint, conversation, and artifact binding |
| `runtime_messages` | one user message and at most one assistant response per run |
| `runtime_run_events` | ordered state transitions, send/response boundaries, and terminal outcome references |
| `runtime_errors` | sanitized provider/transport/configuration/recovery error records |
| `runtime_receipts` | immutable terminal result snapshot for exact replay |

Every ledger table has SQLite triggers rejecting `UPDATE` and `DELETE`. Readers
validate the schema, trigger set, v7.4 delivery, artifact identity, contiguous
state history, terminal outcome, usage arithmetic, and receipt reconstruction.
No runtime operation writes the chat database or semantic, memory, knowledge,
profile, or review sidecars.

## State machine, idempotency, and recovery

The event-derived state machine remains:

`prepared -> running -> succeeded | failed`

Preparation atomically appends the conversation, exact artifact/delivery,
model run, user message, and prepared event. The running event is followed by a
separately committed `send_attempt_started` event before the adapter is called.
A validated provider result is followed by a separately committed
`provider_response_received` event before the assistant message, success event,
usage, and receipt are committed. Failure atomically appends one sanitized
error, failed event, and receipt.

`request_id` and `mutation_id` are independently unique and bound to the
normalized question, budget, artifact identity, and wire-body hash. An exact delayed retry replays the first
immutable receipt, including the original artifact, response/error, usage, and
execution flags. Reusing either ID with different input returns an idempotency
conflict. Concurrent same-key calls create only one run and at most one reply.

A process interruption after `running` is fail-closed: an ordinary retry does
not call the provider again. Recovery distinguishes `interrupted_before_send`,
`interrupted_run_in_doubt`, and `provider_response_persist_in_doubt` from an
explicit provider failure. It appends a failed receipt without re-sending. A
deliberate provider retry uses new request and mutation IDs, preserving both attempts.

The response candidate hook is explicitly `reserved` and `invoked=false`; no
long-term memory candidate is created or approved.

## Production gates and local surfaces

The production runtime sidecar is absent by default. Its initializer is a dry
run unless all production confirmations are explicit:

```powershell
python tools/init_runtime_sidecar.py
python tools/init_runtime_sidecar.py --execute `
  --allow-production-runtime-sidecar `
  --confirm-production-runtime-sidecar-init
```

Normal real-readonly Web mode supports only runtime preview/status GETs.
Execution/recovery POSTs require local fixed-real-source `--runtime-admin` plus
both external-send confirmations. No API accepts a database path, sidecar path,
provider, model, API key, raw delivery, or raw request artifact.

Production execution instances are created only by the internal controlled
factory, which fixes the production sidecar, live source-backed v7.4 builder,
approved-profile projection, and DeepSeek adapter. The injectable test primitive
always rejects the production target, even when passed a valid runtime-admin session.

CLI examples:

```powershell
python tools/runtime_thin_slice.py preview --question "What do I prefer?"
python tools/runtime_thin_slice.py sidecar-status
python tools/runtime_thin_slice.py status --model-run-id <run-id>
```

Real execution is intentionally not part of tests. It additionally requires
`--execute --allow-external-model-call --confirm-deepseek-send`, an initialized
production runtime sidecar, and a locally supplied API key.

Web routes:

- `GET /api/runtime-preview?q=...&budget=Small|Medium|Large`
- `GET /api/runtime-sidecar-status`
- `GET /api/runtime-run?model_run_id=...`
- `POST /api/runtime-execute` in runtime-admin mode only
- `POST /api/runtime-recover` in runtime-admin mode only

Tests use only OS-temporary sidecars and mock adapters. They perform no network
request and do not initialize any production sidecar.
