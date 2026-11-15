# YunOS v8.0-D1 Enablement Blocker Closure

Protocol set:

- Profile control: `v8.0-d1-profile-enablement-control-2`
- Direct proposal: `v8.0-d1-direct-user-profile-proposal-2`
- Profile sidecar: `v8.0-d1-approved-profile-sidecar-2`
- Gate B1 authorization: `v8.0-d1-gate-b1-proposal-authorization-1`
- Gate B2 authorization: `v8.0-d1-gate-b2-decision-authorization-1`
- Active projection: `v8.0-d1-active-approved-profile-1`
- Runtime profile: `v8.0-d1-deepseek-production-profile-1`
- Runtime protocol: `v8.0-d1-deepseek-production-runtime-1`
- Runtime artifact: `v8.0-d1-runtime-request-artifact-1`
- Wire renderer: `v8.0-d1-deepseek-wire-renderer-1`
- Runtime sidecar: `v8.0-d1-runtime-sidecar-1`
- Readiness: `v8.0-d1-deepseek-readiness-2`

The sealed v7.0-v8.0-B/C protocol versions and implementations remain valid
and unchanged. A D1 sidecar deliberately has a different schema marker, so a
sealed v8.0-A or v8.0-B/C reader rejects it instead of silently interpreting a
new record as an old record.

## Direct user profile proposal

The D1 control plane has two explicit subject origins:

1. `v7.2_candidate`: rebuilt and source-backed at decision time;
2. `direct_user_proposal`: typed by the user in explicit profile-admin mode.

A direct proposal is an append-only proposal record, not an approval. It binds:

- proposal protocol and deterministic identity;
- `origin=direct_user_proposal`;
- original normalized text and proposal type;
- `proposed_by=local_user`, fixed by the server rather than supplied by a client;
- optional validated `(conversation_id, message_id)` anchors;
- immutable creation request/mutation IDs, creation-time expected revision,
  one-shot B1 authorization identity, fingerprint, timestamp, and receipt.

Creating a proposal does not increment the decision revision and cannot add an
active item. A separate manual `approve`, `edit_and_approve`, `reject`, or
`defer` decision is required. Candidate and proposal decisions share one
append-only decision chain, expected revision, immutable receipt protocol, and
active projection. Every decision preserves origin, source snapshot, original
text, edited text, final text, decision source, action, and resulting revision.

Production proposal creation and decisions are available only through the
unforgeable profile-admin session and fixed production profile sidecar. The API
accepts neither a database/sidecar path nor a client-supplied user identity.
Normal real-readonly mode rejects every proposal and decision POST. Gate B1 can
only create a proposal. It stops after returning the immutable proposal ID and
cannot decide it. A separately issued Gate B2 capability binds one exact
proposal/candidate ID, action, expected revision, request/mutation IDs, and the
complete edited text plus deterministic hash when applicable. B1 and B2
capabilities are one-shot, non-copyable, non-serializable, and cannot be used
on each other's entry points.

## Fixed DeepSeek production runtime profile

The D1 production profile is immutable:

| Field | Value |
| --- | --- |
| Provider | `deepseek` |
| Model | `deepseek-v4-flash` |
| Base URL | `https://api.deepseek.com` |
| Streaming | `false` |
| Thinking | `{"type":"disabled"}` |

The artifact includes the runtime-profile version/identity, renderer version,
v7.4 Delivery binding, D1 approved-profile projection binding, system
instruction identity, payload, exact canonical UTF-8 wire bytes, and wire
SHA-256. The adapter accepts only the full immutable prepared request and sends
the validated bytes directly; payload-only and changed-byte calls fail closed.

The production runtime factory accepts no adapter, builder, provider, model,
base URL, configuration, raw Delivery, artifact, or sidecar arguments. Its
dependency manifest fixes the production runtime sidecar, source-backed v7.4
builder with D1 projection, and `DeepSeekV4FlashHTTPAdapter`. Test primitives
reject the production runtime path.

The D1 runtime sidecar schema binds the same fixed profile in its immutable
schema row and artifact columns. Initialization is dry-run by default and uses
the same three strict boolean production confirmations as the sealed runtime.

## Gate-E readiness

The readiness command is dry-run by default and reads no credential. Execution
requires three strict boolean confirmations and issues a thread-safe one-shot
capability with `issued`, `consumed`, and `expired` states. The runner atomically
consumes it before reading a credential or constructing transport and can issue
only:

- `GET https://api.deepseek.com/models`
- `GET https://api.deepseek.com/user/balance`

It cannot send a chat completion. Its result contains only target-model
availability, balance availability, controlled status, and endpoint labels. It
does not persist or return a credential, Authorization header, balance amount,
model-list body, or complete provider response.

The production readiness HTTP transport is sealed to the fixed runner's code
identity and has no public adapter constructor. Direct calls fail closed. Tests
use a private transport primitive with an explicitly supplied synthetic
credential and injected opener; that primitive never reads production config.
Consumed or failed capabilities cannot be restored or retried. A later check
requires a new user authorization and a newly issued capability.

```powershell
python tools/deepseek_readiness.py

# Only after an independent Gate-E authorization:
python tools/deepseek_readiness.py --execute `
  --allow-official-readiness-gets `
  --confirm-no-chat-completion
```

## Production default-off state

This implementation does not initialize a production profile/runtime sidecar,
create a real proposal, approve a real profile item, read a production
credential, or perform a network request. Production initialization, readiness,
and the first real turn remain separate gates in the production runbook.
