# ADR-001: Use an Independent Operational State Database

- Status: Accepted for YunOS v9
- Decision date: 2026-07-31
- Authority baseline: `9a1794d8b997e559d33750ee046c6649683aa778`
- Schema generation: `9001`

## Context

YunOS already has four production databases with distinct authorities:

- Main: long-term memory and v8.3 ingestion;
- Profile: approved profile decisions;
- Semantic: derived semantic index;
- Runtime: execution evidence and receipts.

Personal Operational State changes more frequently than long-term memory, needs its own event replay,
projection, snapshot, lease and recovery policy, and must remain readable while its writer is disabled.

## Decision

Create one independent SQLite database at:

`data/operational/ayunos_operational_state.db`

Use append-only `state_events` as the fact source, deterministic `state_projections` as the current
view, and verified snapshots only as replay accelerators. Cross-database relationships are external
IDs and digests, never SQLite cross-database foreign keys.

The first production writer is a local CLI. No v9 browser write endpoint is introduced. Normal Web,
Tailnet and mobile access remain read-only and GET-only.

## Consequences

Positive:

- no Main/Runtime schema pollution;
- independent backup, restore and schema-drift gates;
- projection can be rebuilt without rewriting v7/v8 data;
- production writer can remain off while read APIs are available.

Costs:

- the release backup becomes a five-database set;
- source references need explicit availability and freshness checks;
- operations must coordinate one more SQLite WAL/SHM lifecycle.

## Rejected alternatives

### Add tables to Main

Rejected because it couples volatile current state to long-term memory and v8.3 ingestion recovery.

### Add tables to Runtime

Rejected because Runtime evidence does not equal project state, verified outcome or user acceptance.

### Build a general project-management service

Rejected as out of scope. YunOS v9 is a single-user current-state layer, not a team system.

## Safety constraints

- production creation only in v9-F after backup/rehearsal/restore;
- writer default off;
- state events are append-only;
- candidates never become facts without explicit authority;
- no Agent self-approval or authority expansion;
- no v9-G, v9.1 or v10.
