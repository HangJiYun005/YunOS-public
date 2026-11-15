# YunOS v8.0-D2 Authorization Fidelity Recovery Runbook

Status: reviewed-code preparation only. This document does not authorize or execute R1, R2, or R3.

## Protocol and immutable incident baseline

- Control: `v8.0-d2-authorization-fidelity-control-1`
- Authorization manifest: `v8.0-d2-profile-authorization-manifest-1`
- Append-only overlay schema: `v8.0-d2-authorization-fidelity-overlay-1`
- Batch receipt: `v8.0-d2-authorization-fidelity-batch-receipt-1`
- Production sidecar: fixed internally by the reviewed service; a caller-supplied path is forbidden.
- Incident baseline SHA-256: `0000000000000000000000000000000000000000000000000000000000000000`
- Incident baseline: proposals `4`, decisions `0`, revision `0`, active items `0`.

An authorization cites the ASCII `manifest_identity` and every item text hash. The capability binds the complete canonical manifest bytes, is consumed once, and cannot accept replacement text at execution. A new identity must be presented to and authorized by the user at every stage below.

## Preflight required before every recovery stage

1. Confirm released Git SHA and a clean worktree.
2. Read-only verify the fixed production profile sidecar identity, SQLite integrity, D1 schema/protocol, proposal/decision/revision/active counts, and current SHA-256.
3. Hash the main database and semantic sidecar; record the values for post-stage comparison.
4. Confirm runtime, memory, knowledge, review, and authority sidecars are not being targeted.
5. Before any admin session or capability, call the fixed D2.2 production
   snapshot entrypoint and retain its full result only in process. SQLite BLOB
   values are encoded as deterministic base64 plus byte length and SHA-256;
   TEXT remains separately bound to strict UTF-8 bytes, hash, and code points.
   The snapshot must use protocol
   `v8.0-d2.2-deterministic-sqlite-snapshot-1` and must not be produced by
   passing a database path or SQL identifier from the caller. The privacy-safe
   count/identity probe is `python tools/profile_authorization_snapshot.py`.
6. Build the stage manifest to an ASCII file and independently verify its identity and item hashes. Do not paste authorized Unicode through PowerShell or a terminal code page.
7. Obtain an explicit authorization naming only that stage and its exact manifest identity/hashes.
8. Execute only that stage, take a second fixed read-only snapshot, and call
   `verify_preexisting_rows_unchanged(before, after)`. New append-only rows may
   exist, but every pre-stage row must retain its exact ordering key and row
   identity. Verify the immutable Gate receipt, then stop. Never reuse its
   consumed capability.

Any mismatch stops before append. No recovery stage deletes or updates an existing row.

## R1 / Gate B2: append four corruption rejections

R1 requires a new Gate B2 capability for one all-or-nothing manifest. It appends four individual manual decisions and preserves every original malformed proposal and creation receipt.

The reviewed ASCII source is [v8_0_d2_r1_recovery_source.json](manifests/v8_0_d2_r1_recovery_source.json). It may be rendered only with the offline manifest builder after the preflight.

Fixed bindings:

| Order | Exact subject ID | Action | Reason | Expected revision | Request ID | Mutation ID |
|---:|---|---|---|---:|---|---|
| 1 | `dpp_1dd764316b57786152f854d2ce2e37e55d28af1795e33b724fe54f962f2ce532` | `reject` | `authorization_fidelity_unicode_corruption` | 0 | `gate-d2-r1-reject-01-request` | `gate-d2-r1-reject-01-mutation` |
| 2 | `dpp_3dc813f369f25ec0f013f9c242213564942cd8d363a1be2578702ced2bbd7703` | `reject` | `authorization_fidelity_unicode_corruption` | 1 | `gate-d2-r1-reject-02-request` | `gate-d2-r1-reject-02-mutation` |
| 3 | `dpp_76f052c62f2580c306e8eb5b707b6705ce5d8fff1e2d2b9b6d4780a9af2f5608` | `reject` | `authorization_fidelity_unicode_corruption` | 2 | `gate-d2-r1-reject-03-request` | `gate-d2-r1-reject-03-mutation` |
| 4 | `dpp_1ae0dfe97c8a5e6c2c27a3ae3d5c6b5edd74b8ff5a03895ff85b69293d6e6564` | `reject` | `authorization_fidelity_unicode_corruption` | 3 | `gate-d2-r1-reject-04-request` | `gate-d2-r1-reject-04-mutation` |

The deterministic draft has manifest identity `paf_2a3f6608e7ce9f0d1f27707dd73006e8e364aaa54e41a0514889d68cd2963ff2`; its four edit-text hashes are all `null` because rejection carries no edited text. Each item also binds empty candidate query/budget fields and a null package identity. This identity is valid only if the complete canonical content above is unchanged.

Success evidence:

- Four new append-only decisions with `decision_source=manual` and revisions 1 through 4.
- Proposal count remains 4; decision count becomes 4; active item count remains 0.
- One immutable D2 batch receipt binds the four D1 decision receipt identities.
- Existing proposals and creation receipts remain byte-for-byte represented in the ledger.

Failure recovery: transaction failure leaves the R1 batch absent. A recorded R1 receipt is replayed for the same manifest; a different manifest with the same request or mutation IDs is an idempotency conflict. Stop after verification. Do not enter R2.

## R2 / Gate B1: append four corrected proposals

R2 requires a separate, newly issued Gate B1 capability after R1 is verified at revision 4. The reviewed ASCII-only source is [v8_0_d2_r2_recovery_source.json](manifests/v8_0_d2_r2_recovery_source.json). Generate the authorization envelope with:

```powershell
python tools/build_profile_authorization_manifest.py `
  --input docs/manifests/v8_0_d2_r2_recovery_source.json `
  --output <reviewed-private-output-path>
```

Expected manifest identity:

`paf_b8acf79160eaabcb04902eb1ddbd1e577d5f1a59aa4970df098d0739ce6e77a0`

Exact ordered UTF-8 byte SHA-256 values:

1. `0000000000000000000000000000000000000000000000000000000000000000`
2. `0000000000000000000000000000000000000000000000000000000000000000`
3. `0000000000000000000000000000000000000000000000000000000000000000`
4. `0000000000000000000000000000000000000000000000000000000000000000`

All four items bind `expected_revision=4`, `user_identity=local_user`, empty anchors, exact proposal types, and new IDs `gate-d2-r2-seed-01..04-{request|mutation}`. The source file contains only ASCII JSON and Unicode escapes; the builder decodes strict UTF-8, then records base64 UTF-8 bytes, hashes, and exact code points. Do not replace it with terminal-entered text.

Immediately before R2 authority issuance, capture the fixed D2.2 production
snapshot. The snapshot encoder must consume `canonical_ascii` and every other
BLOB as raw bytes, never pass Python `bytes` directly to `json.dumps`, and
never coerce BLOB to TEXT. If snapshot generation fails, stop before opening
profile-admin mode or creating a capability. After R2, compare the second
snapshot with the retained preflight snapshot to prove every pre-R2 proposal,
receipt, decision, manifest, result, and batch receipt is unchanged.

Success evidence:

- Four new proposals and four creation receipts appear atomically; proposal count becomes 8.
- Decision count and revision remain 4; active item count remains 0.
- Read-back code points and UTF-8 hashes equal the manifest records.
- The immutable batch receipt returns exactly four new `dpp_*` IDs.

Failure recovery: any prewrite, insert, or read-back fidelity mismatch rolls back all four proposals, their receipts, manifest audit rows, and batch receipt. Stop and request a new authorization if manifest content changes. Stop after returning the four new IDs. Do not enter R3.

## R3 / Gate B2: decide only the new exact IDs

R3 cannot be prepared to execution identity until R2 returns its four actual `dpp_*` IDs. A human must choose the action for each returned ID and separately authorize a new Gate B2 manifest binding:

- each exact new proposal ID;
- action (`approve`, `edit_and_approve`, `reject`, or `defer`);
- sequential expected revisions starting from the verified post-R2 revision;
- new request and mutation IDs;
- for `edit_and_approve`, the complete final UTF-8 text record and its hash;
- reason, including an explicit empty reason where applicable.

R3 must reject old malformed IDs unless they are explicitly named in a newly reviewed manifest. It cannot create proposals. Only `approve` or `edit_and_approve` creates a model-visible item; `reject` and `defer` do not. Stop after R3 verification and do not advance to another production Gate.

## Non-actions and privacy boundary

This runbook does not initialize an overlay or any sidecar, create a capability, append a proposal or decision, read a credential, create a network transport, or call a model. Recovery touches only the fixed production profile sidecar after a future independent authorization. Main chat and semantic databases remain read-only; runtime, memory, knowledge, review, and authority sidecars remain out of scope.
