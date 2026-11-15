# YunOS v9-D Delegation Contracts and Outcome Loop

## Boundary

v9-D is not an autonomous agent. It manages only a user-proposed delegation,
its explicit authority, state transitions, referenced runtime evidence,
verification, user acceptance, and candidate state updates.

No v8 Runtime ledger is copied. Outcome records retain only:

- runtime run ID;
- receipt digest;
- verification digest;
- artifact references.

## Delegation Contract

`yunos-delegation-contract-1` binds:

- intent, project, and WorkItem;
- read, write, and tool scopes;
- prohibited operations;
- risk and expiry;
- acceptance criteria and rollback;
- version scope;
- commit, merge, tag, push, and next-node permissions;
- the exact authority digest.

Contracts are canonical, digest-bound, and append-only. A contract cannot
exceed the issuing authority's project, interval, scopes, permissions, or
explicit prohibitions. An agent cannot issue or propose its own contract.

## Overreach Gate

The gate rejects:

- unauthorized node creation;
- version-scope changes;
- read, write, or tool-scope expansion;
- unauthorized commit, merge, tag, or push;
- project completion or seal declarations;
- conflict resolution;
- next-version startup;
- long-term plan creation;
- user-profile updates.

A live delegation overreach creates a `delegation_overreach` event containing
the proposed action and violated authority, records that the operation was not
executed, moves the delegation to `waiting`, and leaves project and WorkItem
completion unchanged.

## Outcome Loop

The implemented flow is:

`Execution Result → Outcome Record → Verification → Acceptance Decision → State Candidate → Projection Update`

Important distinctions:

- `completed` means the runtime/agent reported completion.
- `verified` means referenced evidence passed deterministic verification.
- `accepted` means the user acting under the bound authority accepted the
  verified outcome.

Runtime completion never directly completes a WorkItem. User acceptance creates
a pending `work_item_completion` candidate. Only explicit candidate approval
appends the WorkItem completion event and updates its projection.

Failed execution and failed verification are also preserved. Failed
verification rejects both the outcome and the delegation while retaining the
failure receipt and artifact references.
