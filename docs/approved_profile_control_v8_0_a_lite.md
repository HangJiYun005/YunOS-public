# YunOS v8.0-A Lite Approved Profile Control

## Purpose and boundary

This phase establishes the smallest trusted profile control plane required
before the first real model call. It consumes YunOS v7.2 temporal candidates and
does not change the sealed v7.0, v7.1, v7.2, v7.3, or v7.4 protocols.

Protocol: `v8.0-a-lite-approved-profile-control-2`

Sidecar schema: `v8.0-a-lite-approved-profile-sidecar-2`

Active projection: `v8.0-a-lite-active-approved-profile-2`

The source chat database is always read-only on this surface. The layer makes no
external model/API call, sends nothing to a model, and does not initialize or
write production memory, knowledge, review, or runtime sidecars.

## Review surface

`GET /api/approved-profile-control?q=...&budget=small|medium|large` is available
when `tools/local_web.py` uses the fixed production database in real-readonly
preview or explicit profile-admin mode. Both modes keep the source database
read-only. It invokes the existing fixed production v7.2 builder, which
reconstructs and validates the source-backed v7.0 -> v7.1 -> v7.2 chain.

Each review item includes:

- the complete v7.2 candidate record and package identity;
- v7.1 selection identity, source candidate ID, event ID, and evidence IDs;
- the composite conversation/message anchor and `ayunos://` reference;
- source-excerpt and clause commitments, observation time, and attribution;
- current decision, active state, promoted kind, and allowed actions.
- the complete append-only manual decision history, including source bindings,
  original candidate/evidence snapshots, edited text, revisions, and outcomes.

The original source message remains accessible through the existing composite
`(conversation_id, message_id)` message-context route. A candidate does not
become approved merely because it is displayed.

## Decisions

The supported actions are:

| Action | Effect on active projection |
| --- | --- |
| `approve` | activates the source candidate statement |
| `edit_and_approve` | activates the explicit user-edited statement |
| `reject` | leaves/removes the candidate from the active projection |
| `defer` | leaves/removes the candidate from the active projection |

Candidate kinds compatible with the sealed v7.3 approved-profile input are
projected directly. `acceptable_alternative` is projected as a preference.
`open_question` can only be rejected or deferred.

Ordinary real-readonly mode rejects every POST. Approved-profile writes require
the explicit local-only command `run.ps1 profile-admin`, which fixes the real
source database, opens it read-only, and enables only the approved-profile
decision route. Every accepted profile-admin POST rebuilds the v7.2 package
from the fixed read-only source at execution time and
requires the submitted `package_identity` and `candidate_id` to match it. The
client cannot inject a package, database path, sidecar path, provider, or active
profile item.

Edited statements and reasons are bounded and must pass the inherited v7.0
absolute-path privacy boundary. Active items use the existing sealed v7.3
`make_approved_profile_item` contract, so the projection is already shaped for
later v8.0-B/C consumption without modifying v7.3.

## Append-only record and projection

`approved_profile_decisions` is the only mutable profile ledger. It stores the
request fingerprint, decision and idempotency identities, source package and
selection identities, `decision_source=manual`, candidate/evidence snapshots,
action, revisions, optional
edited statement and reason, resulting approved item, and UTC decision time.

SQLite triggers reject every `UPDATE` and `DELETE`. The active approved profile
is reconstructed from the latest append-only decision for each candidate. There
is no separately writable active table.

Readers recompute the contiguous revision chain, request fingerprint, decision
identity, candidate/evidence bindings, profile-item identity, and privacy-safe
text before returning a projection or accepting another append. Offline row
tampering therefore fails closed even if the SQLite triggers are later restored.

The projection returns context-compatible approved items with explicit
`model_visible=true` plus bindings
to candidate, decision, source package, and resulting revision. `reject` and
`defer` remain in the audit history but are absent from active items.

## Concurrency and idempotency

The sidecar uses `BEGIN IMMEDIATE`. Its current global revision is the maximum
recorded `resulting_revision`, or zero before the first decision.

Every mutation must submit `expected_revision`. The value is checked inside the
write transaction and the next record receives exactly `current + 1`. A stale
page receives `expected_revision_conflict` with HTTP 409 and must reload before
deciding.

`request_id` and `mutation_id` are independently unique. An exact retry with the
same IDs and complete fingerprint returns the original result without another
record. The first transaction also appends an immutable receipt containing the
decision and its resulting active projection. Every exact retry returns that
same complete receipt byte-for-byte, even after later decisions. Reusing either
ID for another payload fails closed with
`idempotency_conflict` and HTTP 409.

## Initialization and production default

The production sidecar path is fixed internally. The initializer is dry-run by
default and creates no parent directory or database:

```powershell
python tools/init_approved_profile_sidecar.py
```

Production execution requires all three explicit flags:

```powershell
python tools/init_approved_profile_sidecar.py --execute `
  --allow-production-profile-sidecar `
  --confirm-production-profile-sidecar-init
```

This command is an enablement gate, not part of normal startup. The Web review
still displays v7.2 candidates when the sidecar is absent, but mutation controls
are disabled and the active projection reports `sidecar_not_initialized`.

Tests use temporary explicitly initialized sidecars and never require the
production sidecar.

The initializer validates `execute`, `allow-production-profile-sidecar`, and
`confirm-production-profile-sidecar-init` as strict booleans. Integers, strings,
or partial confirmation are refused.

## Explicit non-goals

- automatic or shadow approval;
- complex lifecycle UI or full dormant/expired policy;
- multi-device synchronization;
- Runtime or model request assembly;
- DeepSeek API integration;
- chat UI refactoring;
- writes to the real chat database or initialization of production memory,
  knowledge, review, or runtime sidecars.
