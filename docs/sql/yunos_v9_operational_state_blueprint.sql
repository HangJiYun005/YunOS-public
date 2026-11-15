-- DESIGN ONLY
-- NOT A PRODUCTION MIGRATION
-- DO NOT EXECUTE AGAINST ANY YUNOS DATABASE
--
-- YunOS v9 Operational State schema blueprint.
-- The executable migration is implemented and tested separately in v9-A.

PRAGMA foreign_keys = ON;

CREATE TABLE operational_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 9001),
    schema_digest TEXT NOT NULL CHECK (length(schema_digest) = 64),
    projection_version INTEGER NOT NULL CHECK (projection_version = 1),
    created_at TEXT NOT NULL,
    writer_default_enabled INTEGER NOT NULL DEFAULT 0 CHECK (writer_default_enabled = 0)
);

CREATE TABLE state_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'project', 'goal', 'work_item', 'decision', 'commitment',
            'blocker', 'resource', 'delegation', 'outcome'
        )
    ),
    namespace TEXT NOT NULL,
    external_key TEXT NOT NULL,
    identity_digest TEXT NOT NULL CHECK (length(identity_digest) = 64),
    created_event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (entity_type, namespace, external_key)
);

CREATE TABLE state_source_anchors (
    anchor_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    locator TEXT NOT NULL,
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    observed_at TEXT NOT NULL,
    privacy_scope TEXT NOT NULL,
    availability TEXT NOT NULL DEFAULT 'available',
    canonical_json TEXT NOT NULL
);

CREATE TABLE state_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
    entity_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
    authority_kind TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    expires_at TEXT,
    supersedes_event_id TEXT REFERENCES state_events(event_id),
    caused_by_event_id TEXT REFERENCES state_events(event_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    conflict_group_id TEXT,
    canonical_payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    privacy_scope TEXT NOT NULL,
    projection_version INTEGER NOT NULL CHECK (projection_version = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 9001)
);

CREATE TRIGGER state_events_no_update
BEFORE UPDATE ON state_events
BEGIN
    SELECT RAISE(ABORT, 'state_events_append_only');
END;

CREATE TRIGGER state_events_no_delete
BEFORE DELETE ON state_events
BEGIN
    SELECT RAISE(ABORT, 'state_events_append_only');
END;

CREATE TABLE state_projections (
    entity_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
    entity_type TEXT NOT NULL,
    state TEXT NOT NULL,
    freshness TEXT NOT NULL,
    current_event_id TEXT NOT NULL REFERENCES state_events(event_id),
    canonical_projection TEXT NOT NULL,
    projection_digest TEXT NOT NULL CHECK (length(projection_digest) = 64),
    projection_version INTEGER NOT NULL CHECK (projection_version = 1),
    rebuilt_through_sequence INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    through_sequence INTEGER NOT NULL,
    event_chain_digest TEXT NOT NULL CHECK (length(event_chain_digest) = 64),
    canonical_snapshot TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL CHECK (length(snapshot_digest) = 64),
    projection_version INTEGER NOT NULL CHECK (projection_version = 1),
    created_at TEXT NOT NULL
);

CREATE TABLE state_candidates (
    candidate_id TEXT PRIMARY KEY,
    entity_id TEXT REFERENCES state_entities(entity_id),
    candidate_type TEXT NOT NULL,
    canonical_candidate TEXT NOT NULL,
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    confidence_ppm INTEGER NOT NULL CHECK (confidence_ppm BETWEEN 0 AND 1000000),
    source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    expires_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE state_conflicts (
    conflict_group_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
    field_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolved', 'obsolete')),
    canonical_members TEXT NOT NULL,
    conflict_digest TEXT NOT NULL CHECK (length(conflict_digest) = 64),
    resolution_event_id TEXT REFERENCES state_events(event_id),
    created_at TEXT NOT NULL
);

CREATE TABLE state_approvals (
    approval_id TEXT PRIMARY KEY,
    candidate_id TEXT REFERENCES state_candidates(candidate_id),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'revoke')),
    authority_id TEXT NOT NULL,
    authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
    canonical_approval TEXT NOT NULL,
    approval_digest TEXT NOT NULL CHECK (length(approval_digest) = 64),
    recorded_at TEXT NOT NULL
);

CREATE TABLE state_verifications (
    verification_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
    event_id TEXT REFERENCES state_events(event_id),
    verifier_kind TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('passed', 'failed', 'unverifiable')),
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    canonical_verification TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE state_writer_leases (
    lease_name TEXT PRIMARY KEY CHECK (lease_name = 'operational-state-writer'),
    lease_token_digest TEXT NOT NULL CHECK (length(lease_token_digest) = 64),
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE delegation_contracts (
    delegation_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
    project_id TEXT NOT NULL REFERENCES state_entities(entity_id),
    canonical_contract TEXT NOT NULL,
    authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_event_id TEXT NOT NULL REFERENCES state_events(event_id)
);

CREATE TABLE outcome_records (
    outcome_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
    delegation_id TEXT NOT NULL REFERENCES delegation_contracts(delegation_id),
    runtime_run_id TEXT,
    receipt_digest TEXT,
    verification_digest TEXT,
    artifact_references TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reported', 'verified', 'accepted', 'rejected', 'superseded')),
    canonical_outcome TEXT NOT NULL,
    outcome_digest TEXT NOT NULL CHECK (length(outcome_digest) = 64),
    current_event_id TEXT NOT NULL REFERENCES state_events(event_id)
);

CREATE TABLE state_receipts (
    receipt_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL REFERENCES state_events(event_id),
    authority_digest TEXT NOT NULL CHECK (length(authority_digest) = 64),
    preview_digest TEXT NOT NULL CHECK (length(preview_digest) = 64),
    canonical_receipt TEXT NOT NULL,
    receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
    recorded_at TEXT NOT NULL
);

CREATE INDEX state_events_entity_order
ON state_events(entity_id, effective_at, recorded_at, event_id);

CREATE INDEX state_events_type_order
ON state_events(entity_type, event_type, recorded_at, event_id);

CREATE INDEX state_candidates_status_expiry
ON state_candidates(status, expires_at, candidate_id);

CREATE INDEX state_conflicts_status
ON state_conflicts(status, conflict_group_id);
