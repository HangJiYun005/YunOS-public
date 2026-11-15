# YunOS v8.0-D Production Enablement and First Auditable Turn Runbook

Version: `v8.0-d1-phase-0-runbook-3`
Phase 0 snapshot date: `2026-07-15`
Release baseline: `v8.0-bc-deepseek-runtime-thin-slice` at
`58ce19665bfca507ff58abc1c0b8527c028fb18e`

## 1. Scope and non-negotiable boundaries

This runbook prepares production enablement as seven separately authorized
gates. Phase 0 is documentation and read-only preflight only. It does not
initialize a production sidecar, approve a profile item, read a credential,
contact DeepSeek, or run a model.

The intended first-turn provider configuration is fixed:

| Field | Required value |
| --- | --- |
| Provider | `deepseek` |
| Model | `deepseek-v4-flash` |
| Base URL | `https://api.deepseek.com` |
| Streaming | `false` |
| Thinking | `disabled` |

No gate may silently substitute a provider, model, endpoint, streaming mode,
or thinking mode. No gate authorizes the next gate. An operator must stop after
recording the current gate's evidence and obtain the next gate's exact
confirmation text.

The sealed v7.0-v8.0-B/C protocols are immutable inputs. If v8.0-D needs a new
runtime configuration or artifact/schema version, it must be implemented and
independently reviewed as a new v8.0-D change; it must not edit or reinterpret a
sealed protocol in place.

## 2. Phase 0 production snapshot

### 2.1 Git and release state

- `main`, `origin/main`, and this branch's starting point:
  `58ce19665bfca507ff58abc1c0b8527c028fb18e`.
- `main` contains annotated tag `v8.0-a-approved-profile-lite` at
  `5a19c95e0760e24dc4b59d05e96adea7d2b50413`.
- `main` contains annotated tag `v8.0-bc-deepseek-runtime-thin-slice` at
  `58ce19665bfca507ff58abc1c0b8527c028fb18e`.
- Phase 0 began with a clean worktree and `main` ahead/behind `0/0`.

### 2.2 Read-only database baseline

| Store | State | SHA-256 | Logical baseline |
| --- | --- | --- | --- |
| Main chat database | SQLite `quick_check=ok` | `0000000000000000000000000000000000000000000000000000000000000000` | conversations `409`; messages `16324`; DeepSeek conversations `1`; DeepSeek messages `688`; conversation tags `2`; conversation marks `1`; message marks `0` |
| Semantic sidecar | SQLite `quick_check=ok` | `0000000000000000000000000000000000000000000000000000000000000000` | `memory_embeddings=2` |

The main chat database and semantic sidecar must remain byte-identical through
Gates A, B1, B2, C, D, E, F, and G. Any hash change is a stop condition unless a separate, explicitly
authorized migration explains and approves it before continuing.

### 2.3 Production sidecar state

| Sidecar | Phase 0 state |
| --- | --- |
| Approved profile | absent; status `missing`; revision `0`; decisions `0`; active items `0`; `model_visible=false` |
| Runtime | absent; status `missing` |
| Memory | absent |
| Knowledge | absent |
| Review authority registry | absent |
| Review authority ledger | absent |

### 2.4 Source-backed previews

- v7.2 production candidate preview is callable, readonly, candidate-only,
  persisted=false, and uses the local deterministic provider with no external
  calls. The `MacBook` Phase 0 probe selected three evidence items and returned
  a valid partial package, but produced zero candidates.
- Additional bounded probes for preference, goal, study, and cycling terms also
  produced valid source-backed packages but zero candidates. D1 closes this
  enablement gap with separately identified direct user proposals. Gate B1 may
  only create direct proposals; Gate B2 may decide fresh v7.2 candidates,
  returned direct proposal IDs, or both. Every exact subject ID requires the
  separate B2 authorization. Phase 0 does not create a real
  proposal or fabricate a v7.2 candidate.
- v7.4 production Delivery preview is callable and readonly. The `MacBook`
  probe produced delivery identity
  `dvy_d1083d5e455674b1530c11166ab8f73269a6498a5700859f47f5aabee07583c9`,
  assembly identity
  `ctx_b6103571e027f7cd782f5ef4eed94ebedafc430957a7ad2da4ff0a0e58ff5fad`,
  one evidence item, one unapproved candidate item, zero approved-profile
  items, and execution flags persisted=false, sent_to_model=false, and
  external_model_calls=false.
- Runtime preview is callable without creating the runtime sidecar. It reported
  persisted=false, sent_to_model=false, external_model_calls=false, and the
  candidate hook remained `reserved` / `invoked=false`.

### 2.5 Provider profile closure

The sealed v8.0-B/C runtime remains `deepseek-chat`. D1 adds, without changing
that protocol, the independently versioned production profile
`v8.0-d1-deepseek-production-profile-1`, artifact
`v8.0-d1-runtime-request-artifact-1`, renderer
`v8.0-d1-deepseek-wire-renderer-1`, and sidecar
`v8.0-d1-runtime-sidecar-1`. They bind `deepseek-v4-flash`,
`https://api.deepseek.com`, stream=false, and thinking.type=disabled. Gate D
remains blocked until this D1 implementation passes independent review.

### 2.6 Credential entry readiness

- Supported entries: process environment variable `DEEPSEEK_API_KEY` or ignored
  local file `config_private/deepseek.env`.
- Phase 0 observed that the environment variable **name is present** in the
  current process. Its value was not read, printed, hashed, validated, or sent.
- The private file does not currently exist. Its parent directory is ignored by
  Git, and the target file is not tracked.
- Credential presence is not proof of validity, authorization, quota, billing,
  model access, or network readiness.

## 3. Gate A - Initialize the production approved-profile sidecar

### Preconditions

1. `main` remains at or beyond the two sealed release tags above.
2. Worktree is clean; main chat and semantic hashes equal the Phase 0 baseline.
3. Production approved-profile sidecar is absent.
4. No other production sidecar is being initialized concurrently.
5. A named operator has reviewed the dry-run output and the exact target path.

### Independent confirmation text

> I authorize Gate A only: initialize the production approved-profile sidecar at the fixed YunOS production path. Do not approve any candidate, do not initialize any other sidecar, and stop after recording initialization evidence.

### Execution action

1. Run the initializer without `--execute` and retain its dry-run result.
2. Confirm the fixed target and expected empty schema.
3. Run:

```powershell
python tools/init_profile_enablement_sidecar.py --execute `
  --allow-production-profile-sidecar `
  --confirm-production-profile-sidecar-init
```

### Success evidence

- Status is initialized/ready at the fixed production path.
- Schema and append-only triggers validate.
- Decision count, revision, and active item count are all zero.
- Main chat and semantic hashes are unchanged.
- Runtime, memory, knowledge, and review sidecars remain absent.

### Failure recovery

- Stop immediately; do not rerun with altered paths or relaxed validation.
- Record the controlled reason code and file-existence state without dumping SQL
  or local secrets.
- If a partial file exists, do not delete or repair it in place. Preserve it for
  forensic inspection and obtain a separate recovery authorization.

### Stop rule

Gate A never starts Gate B1 automatically.

## 4. Gate B1 - Create direct user proposals only

### Preconditions

1. Gate A evidence is accepted and the profile sidecar is empty and valid.
2. The operator has prepared the exact original text, type, optional composite
   source anchors, fixed `local_user` identity, request ID, mutation ID, and
   current expected revision for every proposal to be created.
3. Each proposed item is short, deterministic, user-attributed, and is not an
   assistant inference, secret, open question, or absolute path.
4. Profile-admin uses the fixed readonly source and fixed D1 profile sidecar.

### Independent confirmation text

> I authorize Gate B1 only: create exactly the listed direct user proposals using their recorded original text, type, optional conversation_id + message_id anchors, local_user identity, request_id, mutation_id, and expected revision. Do not approve, edit-and-approve, reject, defer, or change the active profile. Stop after returning the immutable creation receipts and real proposal IDs.

The confirmation must reproduce every B1 field exactly. A broad instruction to
"create my profile" or a prior Gate authorization is insufficient.

### Execution action

1. Start `run.ps1 profile-admin` locally.
2. Submit only the authorized proposal creations, one at a time.
3. Record each immutable B1 receipt and returned `dpp_*` proposal ID.
4. Verify decision revision and active projection are unchanged.
5. Stop. Do not reload and decide a proposal under the B1 authorization.

### Success evidence

- Only append-only proposal rows and immutable B1 receipts were added.
- Each receipt binds original text, type, `local_user`, optional validated
  anchors, expected revision, request/mutation IDs, authorization identity, and
  real proposal ID.
- Decision count, active items, and model-visible state are unchanged.

### Failure recovery

- Stop on revision, identity, anchor, or idempotency conflict.
- A consumed capability is never reused. An uncertain identical mutation needs
  a new B1 authorization carrying the same immutable payload and IDs.
- Do not approve or repair rows in place.

### Stop rule

Gate B1 never starts Gate B2 automatically. Return the exact proposal IDs and
wait for a new user authorization.

## 5. Gate B2 - Decide exact proposal or candidate IDs only

### Preconditions

1. Gate B1 has stopped and its creation receipts have been independently
   reviewed, or the chosen subjects are fresh source-backed v7.2 candidates.
2. The authorization manifest lists exactly three to five subject IDs, each
   labeled `direct_user_proposal` or `v7.2_candidate`.
3. Every entry includes its exact ID, action, expected revision, request ID,
   mutation ID, and complete final edited text plus SHA-256 when action is
   `edit-and-approve`.
4. Every candidate is freshly rebuilt from its source-backed v7.2 package.

### Independent confirmation text

> I authorize Gate B2 only: manually decide exactly the listed proposal/candidate IDs with their recorded origin, action, expected_revision, request_id, mutation_id, and complete edited text/hash where applicable. Do not create proposals, do not decide any unlisted ID, and stop after the listed append-only decisions and active projection are recorded.

### Execution action

1. Reload the review after B1 and verify every returned proposal ID.
2. Submit only the exact B2 manifest entries, one at a time.
3. For a candidate, rebuild and validate the source-backed v7.2 package at the
   write boundary.
4. Stop after the authorized three to five decisions.

### Success evidence

- Every decision has a one-shot B2 authorization identity and
  `decision_source=manual`.
- ID, action, revision, edit text/hash, request ID, mutation ID, source evidence,
  and immutable receipt all match the authorization manifest.
- Only approve/edit-and-approve entries are model-visible; reject/defer are not.
- No proposal row was created during Gate B2.

### Failure recovery

- Stop on any binding or revision mismatch; the mismatched capability expires.
- A consumed capability is never reused. Idempotent replay requires a newly
  authorized B2 capability with the identical payload and IDs.
- Do not process a new proposal ID until it appears in a new B2 authorization.

### Stop rule

Gate B2 never starts Gate C automatically.

## 6. Gate C - Verify active profile and regenerate v7.4 Delivery

### Preconditions

1. Gate B2 has an accepted decision manifest and exactly three to five active
   model-visible items.
2. No profile decision is in flight.
3. Main chat and semantic hashes still equal the Phase 0 baseline.
4. The first-turn question candidate has been selected but not sent.

### Independent confirmation text

> I authorize Gate C only: perform readonly verification of the active approved-profile projection and regenerate v7.4 Delivery for the selected first-turn question. Do not mutate the profile, initialize runtime, read a key, or contact a provider.

### Execution action

1. Read the active projection and record projection identity, revision, decision
   count, item IDs, item count, and model-visible state.
2. Generate a fresh v7.4 Delivery preview for the exact selected question and
   fixed budget.
3. Run `python tools/runtime_enablement.py preview --question <QUESTION>` and
   verify the D1 fixed runtime profile while retaining execution flags false.
4. Record delivery identity, assembly identity, model-context hash/length,
   approved item IDs, evidence anchors, omissions, and readonly execution flags.
5. Do not print full model context in routine logs.

### Success evidence

- Projection reconstructs from the append-only decision log.
- Exactly the authorized profile items appear in the approved layer.
- No unapproved candidate appears as an approved profile fact.
- Delivery is source-backed, readonly, persisted=false, sent_to_model=false,
  and external_model_calls=false.

### Failure recovery

- Stop on projection, source, revision, identity, or item-set mismatch.
- Do not patch Delivery or profile rows. Return to Gate B2 only with new explicit
  Gate B2 authorization if another append-only decision is required.

### Stop rule

Gate C never starts Gate D automatically.

## 7. Gate D - Initialize the production runtime sidecar

### Preconditions

1. Gate C evidence is accepted.
2. The versioned and independently reviewed v8.0-D1 runtime implementation binds
   exactly: deepseek / deepseek-v4-flash /
   https://api.deepseek.com / stream=false / thinking=disabled.
3. Runtime artifact, wire renderer, adapter, schema, CLI/API, privacy, audit,
   mock-provider tests, failure recovery, and production factory all agree on
   that configuration.
4. Production runtime sidecar is absent and the approved-profile sidecar is
   valid.
5. D1 code implements prerequisite 2, but Gate D remains blocked until its
   independent review is accepted.

### Independent confirmation text

> I authorize Gate D only: initialize the production runtime sidecar for the independently reviewed v8.0-D1 deepseek-v4-flash configuration at the fixed production path. Do not read a key, send a request, or initialize any other sidecar, and stop after schema evidence is recorded.

### Execution action

1. Run the runtime initializer without `--execute` and retain the dry-run.
2. Confirm the fixed path, schema version, and exact model/config constraints.
3. Run:

```powershell
python tools/init_runtime_enablement_sidecar.py --execute `
  --allow-production-runtime-sidecar `
  --confirm-production-runtime-sidecar-init
```

### Success evidence

- Runtime status is initialized/ready at the fixed production path.
- Schema and append-only triggers validate against the reviewed v8.0-D version.
- There are zero conversations, messages, artifacts, runs, events, errors, and
  receipts.
- Main chat, semantic, approved-profile, memory, knowledge, and review stores
  have no unexpected change.

### Failure recovery

- Stop; do not downgrade the schema or initialize through a test primitive.
- Preserve any partial file for inspection and require separate recovery
  authorization before deletion, replacement, or retry.

### Stop rule

Gate D never starts Gate E automatically.

## 8. Gate E - Configure the credential and prepare network/authentication

### Preconditions

1. Gates A-D evidence is accepted.
2. The operator has authority to use the intended DeepSeek account, model,
   quota, billing scope, and network.
3. The key will be entered only through `DEEPSEEK_API_KEY` or the ignored
   `config_private/deepseek.env` file.
4. Logs, shell history, screen sharing, diagnostics, and error capture are
   configured not to reveal the value.
5. The only permitted readiness endpoints are official `GET /models` and
   `GET /user/balance`; they send no user question, YunOS Delivery, profile,
   chat text, or model prompt.

### Independent confirmation text

> I authorize Gate E only: configure the DeepSeek credential through the approved private entry and perform exactly one GET /models and one GET /user/balance readiness check against https://api.deepseek.com. Do not call chat/completions, do not send any question or YunOS context, do not persist the key, Authorization header, balance amount, model-list body, or complete response, and stop after redacted readiness evidence is recorded.

### Execution action

1. The operator configures the private entry locally; automation must not echo,
   read back, hash, print, log, or commit the value.
2. Confirm only entry presence, Git-ignore state, target base URL, TLS policy,
   and reviewed model identifier.
3. Run `python tools/deepseek_readiness.py` first and retain its no-network dry
   run. It must report only `GET /models` and `GET /user/balance`.
4. After this Gate's authorization, execute once with
   `--execute --allow-official-readiness-gets --confirm-no-chat-completion` and
   retain only redacted availability/status/timestamp evidence.
5. The readiness capability is atomically consumed before credential access.
   It cannot be copied, restored, or reused; either GET failing still consumes
   it. A second readiness attempt requires a new Gate E authorization.

### Success evidence

- Private entry present and untracked; no key literal in Git, logs, database,
  receipt, artifact, frontend, or error output.
- Base URL and TLS validation match policy.
- Reviewed authentication probe, when required, succeeds without sending YunOS
  content; model access/quota status is recorded without response-body leakage.

### Failure recovery

- Stop on missing/invalid credential, TLS, DNS, authorization, quota, billing,
  or model-access failure.
- Do not retry automatically or reuse the consumed capability. Revoke/replace the credential outside YunOS if
  exposure is suspected, clear only authorized local process state, and obtain
  new Gate E authorization.

### Stop rule

Gate E never starts Gate F automatically.

## 9. Gate F - Authorize the first real non-streaming turn

### Preconditions

1. Gates A-E evidence is complete and accepted.
2. Production profile contains exactly the approved three to five items.
3. Runtime sidecar is empty and valid.
4. Exact question, budget, request ID, mutation ID, provider configuration, and
   comparison plan are recorded.
5. A fresh readonly Delivery/Runtime preview has persisted=false,
   sent_to_model=false, and external_model_calls=false.
6. Main chat and semantic hashes equal the Phase 0 baseline.
7. No other runtime-admin executor is active.

### Independent confirmation text

> I authorize Gate F only: send exactly the recorded first question once using request_id `<REQUEST_ID>` and mutation_id `<MUTATION_ID>` with provider deepseek, model deepseek-v4-flash, base URL https://api.deepseek.com, stream=false, and thinking=disabled. Persist only to the production runtime sidecar. Do not retry automatically, do not send any other question, and stop immediately after the immutable result or in-doubt state is recorded.

The placeholders must be replaced with final IDs, and the exact question must
be reproduced immediately above the confirmation.

### Execution action

1. Start fixed local runtime-admin mode or use the reviewed execution CLI.
2. Execute one request with the recorded question, budget, request ID, mutation
   ID, and all three strict execution confirmations.
3. Do not refresh, repeat, change IDs, or submit another question while the run
   is running or in doubt.

### Success evidence

- Exactly one model run and one external call boundary.
- Append-only events show prepared, running, send attempt, provider response,
  and succeeded in the reviewed order.
- One assistant message, normalized usage, immutable receipt, matching artifact
  identity, and matching wire-body hash.
- Candidate hook remains reserved/invoked=false.

### Failure recovery

- Before-send interruption may be explicitly recovered as not sent.
- After send-attempt or provider-response evidence, ordinary retry is forbidden.
- Use explicit recovery only after separately confirming the run ID; recovery
  must not call the provider and must preserve the appropriate in-doubt state.

### Stop rule

Gate F never starts Gate G automatically and never sends a second turn.

## 10. Gate G - Verify the first run and production zero-anomaly state

### Preconditions

1. Gate F has a terminal receipt or an explicitly identified running/in-doubt
   run.
2. The exact pre-turn database hashes and sidecar inventories are available.
3. Verification is readonly except for an explicitly authorized recovery of a
   running run; no resend is permitted.

### Independent confirmation text

> I authorize Gate G only: perform readonly verification of model_run `<MODEL_RUN_ID>`, its Delivery, artifact, wire hash, events, usage, answer, source anchors, receipt, and production database invariants. Do not send or retry a model request. If recovery is required, stop and request a separate explicit recovery confirmation.

### Execution action

1. Read the run through the production status surface and independently inspect
   the runtime sidecar in readonly mode.
2. Recompute run, message, event, artifact, wire, error/usage, and receipt
   identities.
3. Verify source Delivery identity, assembly identity, approved-profile
   projection identity/revision/item IDs, model configuration, request IDs, and
   question binding.
4. Compare the answer for groundedness and source coverage; do not promote it to
   memory or profile.
5. Recompute main chat and semantic hashes and inventory every production
   sidecar.

### Success evidence

- One coherent run, one external call, at most one assistant message, exact
  immutable receipt replay, and valid usage arithmetic.
- Captured HTTP body hash equals artifact `wire_body_sha256`.
- Answer and cited source anchors are reviewable; uncertainty is explicit where
  evidence is insufficient.
- Main chat and semantic hashes are unchanged; no memory, knowledge, or review
  sidecar appeared; approved-profile state is unchanged; runtime contains only
  the authorized first turn.

### Failure recovery

- Classify the run as explicit failure, send-attempt in-doubt, or
  provider-response persistence in-doubt. Never infer that an uncertain request
  was unsent.
- Preserve all append-only evidence. Do not UPDATE/DELETE rows or submit a
  second call. Produce an incident record and obtain separate authorization for
  any recovery or later retry with new IDs.

### Stop rule

Gate G ends the first-turn procedure. No second turn, candidate generation,
profile update, or memory promotion is authorized.

## 11. Candidate first-turn questions - do not execute in Phase 0

### Recommended question

> Based on my prior YunOS history about choosing a MacBook, what preferences or exclusions are actually supported, and which points remain uncertain? Cite the relevant conversation and message anchors, and distinguish approved-profile facts from historical evidence.

Why this is preferred: a bare model should not know the user's private decision
history, while YunOS can demonstrate source-backed Delivery, trust-layer
separation, approved-profile injection, uncertainty, and composite anchors in a
single bounded turn. Phase 0 has not asserted what the answer is.

### Alternates

1. Based on my prior cycling and weight-management discussions, what durable
   goals and constraints are supported, and which are only tentative?
2. Across my YunOS architecture discussions, which design principles have I
   repeatedly treated as non-negotiable, with source anchors?
3. Based on my study-planning history, what learning priorities and constraints
   are supported, and where is the evidence conflicting or incomplete?

Each question must first produce at least one relevant, source-backed v7.4
Delivery. If its evidence is empty, unrelated, or too broad, reject the question
and select another; do not compensate with client-provided facts.

## 12. Three-way comparison plan

Use the same exact question and the same reviewed
deepseek-v4-flash / non-streaming / thinking-disabled generation configuration.
Run only after Gate F authorization, with separate request/mutation IDs and
clear labels so no result is mistaken for another.

| Arm | Input | Purpose | Required controls |
| --- | --- | --- | --- |
| A - Bare DeepSeek | Exact question only; no YunOS profile/history | Establish what the provider can answer without private history | Must not claim private facts; record uncertainty, usage, and response identity |
| B - Manual background | Exact question plus a short, user-reviewed background block | Compare against deliberately supplied context | Record the exact background/hash; no database lookup or hidden YunOS Delivery |
| C - YunOS Delivery | Exact question through the source-backed v7.4 Delivery and approved-profile projection | Test the auditable YunOS advantage | Record Delivery/assembly/profile/artifact identities, wire hash, usage, anchors, omissions, and receipt |

Compare grounded facts, unsupported claims, uncertainty, source traceability,
approved-vs-unapproved separation, input/output usage, and operator reviewability.
Do not use an answer from one arm as input to another. The bare and manual arms
must also use controlled runtime records or an explicitly approved comparison
sidecar; they must never write the main chat database.

## 13. Phase 0 completion criteria

Phase 0 is complete when this runbook is versioned, privacy/audit checks pass,
Git diff is cleanly reviewable, production databases match the snapshot, and no
production sidecar or external request has been created. Completion of Phase 0
does not mean production is enabled. Current blockers are:

1. D1 B1/B2 capabilities close the zero-candidate enablement gap without
   relaxing v7.2, combining creation with approval, or allowing automatic
   approval; no real proposals exist yet;
2. D1 implements the fixed v4-flash profile/artifact/renderer/schema and the
   one-shot sealed Gate E readiness boundary, but production gates remain
   blocked until independent review accepts this revision;
3. runtime and profile sidecars remain intentionally absent;
4. credential presence has not been validated for correctness, authorization,
   quota, billing, model access, or network access.
