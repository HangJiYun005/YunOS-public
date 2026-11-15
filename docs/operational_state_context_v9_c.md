# YunOS v9-C Current Context and Resume Engine

## Contracts

The v9-C reader exposes two deterministic private-state packages:

- `yunos-now-package-1`
- `yunos-project-resume-package-1`

Both are built only from the fifth operational-state database. They are
read-only, source-backed, digest-bound, and use the v7.1 evidence-selection
budget profiles and token estimator. Candidates remain separately labelled
`candidate_not_fact`.

Live private-state envelopes declare:

- `cache_control: no-store`
- `cache_classification: private_operational_state_live`
- `cached_at: null`
- `cache_stale: false`
- `source_state: live`

If a future v8.2 private offline cache stores one of these packages, it must use
a distinct cached classification and populate `cached_at` and `cache_stale`.
The live reader never labels cached data as live.

## Now Package

The Now Package includes a bounded current focus, active goals and projects,
blockers, waiting work, delegations, pending decisions, recent changes,
non-fresh state, conflicts, resources, constraints, candidate-only items,
source anchors, every omission reason, the applied budget, and a deterministic
package digest.

It applies explicit section caps to active projects, recent changes, and source
anchors. A package therefore cannot silently expand into every project, every
event, every chat, or the complete database. Stale and conflict totals remain
visible even when a small budget omits individual entries.

## Project Resume Package

The Project Resume Package includes:

- purpose, authoritative baseline, current version, current phase, state, and
  freshness;
- completed and current work;
- approved and candidate next steps;
- approved and pending decisions;
- blockers, waiting work, delegations, outcomes, and conflicts;
- deferred and out-of-scope work;
- prohibited actions;
- source anchors, omissions, budget, and digest.

## Read-Only HTTP API

The existing local service adds only GET dispatch for:

- `/api/operational-state/status`
- `/api/operational-state/now`
- `/api/operational-state/projects`
- `/api/operational-state/project`
- `/api/operational-state/resume`
- `/api/operational-state/waiting`
- `/api/operational-state/decisions`
- `/api/operational-state/delegations`
- `/api/operational-state/conflicts`
- `/api/operational-state/freshness`
- `/api/operational-state/evidence`

Every JSON response already receives the service-wide
`Cache-Control: no-store` header. No operational-state POST route exists.
Unknown parameters fail closed. Before v9-F creates the production database,
the status endpoint reports a path-redacted, writer-disabled unavailable state.
