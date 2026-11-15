-- YunOS v10 bounded cognitive agency schema generation 10001.
-- Executable additive DDL for a verified v9 Operational State database.
-- The migration runner supplies the transaction, lease gate, schema digests,
-- cognitive_schema marker row, rollback, and production-path prohibition.

CREATE TABLE cognitive_schema (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    marker TEXT NOT NULL UNIQUE CHECK (
        marker = 'yunos-bounded-cognitive-agency-10001'
    ),
    schema_generation INTEGER NOT NULL CHECK (schema_generation = 10001),
    schema_digest TEXT NOT NULL CHECK (
        length(schema_digest) = 64
        AND schema_digest NOT GLOB '*[^0-9a-f]*'
    ),
    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
    created_at TEXT NOT NULL,
    writer_default_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (writer_default_enabled = 0)
);

CREATE TABLE objective_contracts (
    objective_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    project_id TEXT NOT NULL,
    agency_level TEXT NOT NULL CHECK (
        agency_level IN ('consult', 'plan', 'prepare', 'act')
    ),
    cognitive_stance TEXT NOT NULL CHECK (
        cognitive_stance IN ('continue', 'explore', 'breakthrough')
    ),
    clarification_required INTEGER NOT NULL CHECK (
        clarification_required IN (0, 1)
    ),
    canonical_contract TEXT NOT NULL CHECK (json_valid(canonical_contract)),
    canonical_digest TEXT NOT NULL CHECK (
        length(canonical_digest) = 64
        AND canonical_digest NOT GLOB '*[^0-9a-f]*'
    ),
    source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
    created_event_id TEXT NOT NULL UNIQUE REFERENCES state_events(event_id),
    approval_id TEXT REFERENCES state_approvals(approval_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (objective_id, version),
    UNIQUE (objective_id, canonical_digest)
);

CREATE TABLE provider_registry (
    provider_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
    display_name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    manual_selection_only INTEGER NOT NULL DEFAULT 1
        CHECK (manual_selection_only = 1),
    secret_reference TEXT,
    health_state TEXT NOT NULL CHECK (
        health_state IN (
            'unknown', 'healthy', 'degraded', 'unavailable',
            'disabled'
        )
    ),
    canonical_profile TEXT NOT NULL CHECK (json_valid(canonical_profile)),
    profile_digest TEXT NOT NULL CHECK (
        length(profile_digest) = 64
        AND profile_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (provider_id, profile_version),
    UNIQUE (provider_id, profile_digest),
    CHECK (
        secret_reference IS NULL
        OR (
            length(secret_reference) BETWEEN 3 AND 191
            AND secret_reference NOT LIKE '%sk-%'
            AND secret_reference NOT LIKE '%Bearer %'
        )
    )
);

CREATE TRIGGER provider_registry_no_update
BEFORE UPDATE ON provider_registry
BEGIN
    SELECT RAISE(ABORT, 'provider_registry_append_only');
END;

CREATE TRIGGER provider_registry_no_delete
BEFORE DELETE ON provider_registry
BEGIN
    SELECT RAISE(ABORT, 'provider_registry_append_only');
END;

CREATE TABLE capability_registry (
    capability_id TEXT NOT NULL,
    capability_version INTEGER NOT NULL CHECK (capability_version >= 1),
    display_name TEXT NOT NULL,
    risk_class TEXT NOT NULL CHECK (
        risk_class IN ('low', 'medium', 'high', 'critical')
    ),
    reversible INTEGER NOT NULL CHECK (reversible IN (0, 1)),
    required_authority TEXT NOT NULL CHECK (
        required_authority IN ('none', 'read', 'prepare', 'explicit_act')
    ),
    executor_kind TEXT NOT NULL CHECK (
        executor_kind IN (
            'native', 'runtime', 'codex', 'external_agent', 'local_broker'
        )
    ),
    availability TEXT NOT NULL CHECK (
        availability IN ('available', 'unavailable', 'disabled', 'handoff_only')
    ),
    timeout_ms INTEGER NOT NULL CHECK (
        timeout_ms BETWEEN 1 AND 1200000
    ),
    max_output_bytes INTEGER NOT NULL CHECK (
        max_output_bytes BETWEEN 1 AND 104857600
    ),
    canonical_capability TEXT NOT NULL CHECK (json_valid(canonical_capability)),
    capability_digest TEXT NOT NULL CHECK (
        length(capability_digest) = 64
        AND capability_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (capability_id, capability_version),
    UNIQUE (capability_id, capability_digest),
    UNIQUE (capability_id, capability_version, capability_digest)
);

CREATE TABLE plan_graphs (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    objective_id TEXT NOT NULL,
    objective_version INTEGER NOT NULL,
    objective_digest TEXT NOT NULL CHECK (length(objective_digest) = 64),
    canonical_graph TEXT NOT NULL CHECK (json_valid(canonical_graph)),
    plan_digest TEXT NOT NULL CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_event_id TEXT NOT NULL REFERENCES state_events(event_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, version),
    UNIQUE (plan_id, plan_digest),
    FOREIGN KEY (objective_id, objective_version)
        REFERENCES objective_contracts(objective_id, version)
);

CREATE TABLE plan_nodes (
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_kind TEXT NOT NULL CHECK (
        node_kind IN (
            'action', 'decision', 'human_gate', 'rollback',
            'alternative', 'verification'
        )
    ),
    risk_class TEXT NOT NULL CHECK (
        risk_class IN ('low', 'medium', 'high', 'critical')
    ),
    reversible INTEGER NOT NULL CHECK (reversible IN (0, 1)),
    authority_requirement TEXT NOT NULL CHECK (
        authority_requirement IN (
            'none', 'read', 'prepare', 'explicit_act', 'human_gate'
        )
    ),
    timeout_ms INTEGER NOT NULL CHECK (
        timeout_ms BETWEEN 1 AND 1200000
    ),
    state TEXT NOT NULL CHECK (
        state IN (
            'proposed', 'ready', 'running', 'waiting_authority',
            'waiting_external', 'verifying', 'completed', 'failed',
            'cancelled', 'omitted', 'rollback_required', 'rolled_back'
        )
    ),
    canonical_node TEXT NOT NULL CHECK (json_valid(canonical_node)),
    node_digest TEXT NOT NULL CHECK (length(node_digest) = 64),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (plan_id, plan_version, node_id),
    UNIQUE (plan_id, plan_version, ordinal),
    FOREIGN KEY (plan_id, plan_version)
        REFERENCES plan_graphs(plan_id, version)
);

CREATE TABLE plan_edges (
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    edge_id TEXT NOT NULL,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL CHECK (
        edge_kind IN (
            'sequence', 'parallel', 'decision', 'failure',
            'rollback', 'alternative', 'resume'
        )
    ),
    condition_json TEXT CHECK (
        condition_json IS NULL OR json_valid(condition_json)
    ),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0),
    edge_digest TEXT NOT NULL CHECK (length(edge_digest) = 64),
    PRIMARY KEY (plan_id, plan_version, edge_id),
    FOREIGN KEY (plan_id, plan_version, from_node_id)
        REFERENCES plan_nodes(plan_id, plan_version, node_id),
    FOREIGN KEY (plan_id, plan_version, to_node_id)
        REFERENCES plan_nodes(plan_id, plan_version, node_id),
    CHECK (from_node_id <> to_node_id)
);

CREATE TABLE plan_revisions (
    revision_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    from_version INTEGER,
    to_version INTEGER NOT NULL CHECK (to_version >= 1),
    change_reason TEXT NOT NULL,
    authority_impact TEXT NOT NULL CHECK (
        authority_impact IN ('none', 'narrows_scope', 'requires_new_authority')
    ),
    previous_digest TEXT,
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    canonical_revision TEXT NOT NULL CHECK (json_valid(canonical_revision)),
    revision_digest TEXT NOT NULL UNIQUE CHECK (length(revision_digest) = 64),
    created_event_id TEXT NOT NULL REFERENCES state_events(event_id),
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, to_version),
    FOREIGN KEY (plan_id, to_version)
        REFERENCES plan_graphs(plan_id, version),
    FOREIGN KEY (plan_id, from_version)
        REFERENCES plan_graphs(plan_id, version),
    CHECK (
        (
            from_version IS NULL
            AND previous_digest IS NULL
            AND to_version = 1
        )
        OR (
            from_version IS NOT NULL
            AND previous_digest IS NOT NULL
            AND to_version = from_version + 1
        )
    )
);

CREATE TABLE context_bindings (
    binding_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    objective_version INTEGER NOT NULL,
    source_system TEXT NOT NULL CHECK (
        source_system IN (
            'v7_memory', 'v9_now', 'v9_resume', 'project', 'runtime'
        )
    ),
    source_reference TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    maximum_items INTEGER NOT NULL CHECK (maximum_items >= 1),
    maximum_bytes INTEGER NOT NULL CHECK (maximum_bytes >= 256),
    selected_items INTEGER NOT NULL CHECK (
        selected_items BETWEEN 0 AND maximum_items
    ),
    canonical_bytes INTEGER NOT NULL CHECK (
        canonical_bytes BETWEEN 0 AND maximum_bytes
    ),
    freshness TEXT NOT NULL CHECK (
        freshness IN ('fresh', 'aging', 'stale', 'unverifiable')
    ),
    contains_secret INTEGER NOT NULL DEFAULT 0 CHECK (contains_secret = 0),
    canonical_binding TEXT NOT NULL CHECK (json_valid(canonical_binding)),
    binding_digest TEXT NOT NULL UNIQUE CHECK (length(binding_digest) = 64),
    source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id, objective_version)
        REFERENCES objective_contracts(objective_id, version)
);

CREATE TABLE authorization_artifacts (
    authorization_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    objective_version INTEGER NOT NULL,
    objective_digest TEXT NOT NULL CHECK (length(objective_digest) = 64),
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    project_id TEXT NOT NULL,
    delegation_id TEXT NOT NULL REFERENCES delegation_contracts(delegation_id),
    delegation_digest TEXT NOT NULL CHECK (
        length(delegation_digest) = 64
        AND delegation_digest NOT GLOB '*[^0-9a-f]*'
    ),
    source_authority_id TEXT NOT NULL,
    source_authority_digest TEXT NOT NULL CHECK (
        length(source_authority_digest) = 64
    ),
    issuer_kind TEXT NOT NULL CHECK (issuer_kind = 'user'),
    issuer_id TEXT NOT NULL,
    nonce_digest TEXT NOT NULL UNIQUE CHECK (length(nonce_digest) = 64),
    risk_ceiling TEXT NOT NULL CHECK (
        risk_ceiling IN ('low', 'medium', 'high', 'critical')
    ),
    max_steps INTEGER NOT NULL CHECK (max_steps >= 1),
    max_duration_ms INTEGER NOT NULL CHECK (
        max_duration_ms BETWEEN 1 AND 86400000
    ),
    max_model_calls INTEGER NOT NULL CHECK (max_model_calls >= 0),
    max_tokens INTEGER NOT NULL CHECK (max_tokens >= 0),
    max_cost_microunits INTEGER NOT NULL CHECK (max_cost_microunits >= 0),
    commit_permission INTEGER NOT NULL DEFAULT 0
        CHECK (commit_permission IN (0, 1)),
    merge_permission INTEGER NOT NULL DEFAULT 0
        CHECK (merge_permission = 0),
    tag_permission INTEGER NOT NULL DEFAULT 0
        CHECK (tag_permission = 0),
    push_permission INTEGER NOT NULL DEFAULT 0
        CHECK (push_permission = 0),
    network_permission INTEGER NOT NULL DEFAULT 0
        CHECK (network_permission = 0),
    database_permission TEXT NOT NULL DEFAULT 'none'
        CHECK (database_permission IN ('none', 'readonly')),
    next_version_creation_permission INTEGER NOT NULL DEFAULT 0
        CHECK (next_version_creation_permission = 0),
    persistent_background_agent_permission INTEGER NOT NULL DEFAULT 0
        CHECK (persistent_background_agent_permission = 0),
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    canonical_authorization TEXT NOT NULL CHECK (
        json_valid(canonical_authorization)
    ),
    authority_digest TEXT NOT NULL UNIQUE CHECK (
        length(authority_digest) = 64
        AND authority_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_event_id TEXT NOT NULL REFERENCES state_events(event_id),
    created_at TEXT NOT NULL,
    FOREIGN KEY (objective_id, objective_version)
        REFERENCES objective_contracts(objective_id, version),
    FOREIGN KEY (plan_id, plan_version)
        REFERENCES plan_graphs(plan_id, version),
    CHECK (expires_at > valid_from)
);

CREATE TABLE cognitive_runs (
    run_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    objective_version INTEGER NOT NULL,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    provider_profile_version INTEGER NOT NULL,
    authorization_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'reasoning', 'planning', 'waiting_authority',
            'executing', 'waiting_external', 'verifying',
            'waiting_acceptance', 'completed', 'failed',
            'cancelled', 'recovery_required'
        )
    ),
    checkpoint_sequence INTEGER NOT NULL DEFAULT 0
        CHECK (checkpoint_sequence >= 0),
    last_safe_checkpoint_digest TEXT,
    model_calls_used INTEGER NOT NULL DEFAULT 0 CHECK (model_calls_used >= 0),
    tokens_used INTEGER NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
    cost_used_microunits INTEGER NOT NULL DEFAULT 0
        CHECK (cost_used_microunits >= 0),
    steps_used INTEGER NOT NULL DEFAULT 0 CHECK (steps_used >= 0),
    started_at TEXT,
    last_activity_at TEXT,
    ended_at TEXT,
    failure_code TEXT,
    current_projection_digest TEXT NOT NULL CHECK (
        length(current_projection_digest) = 64
    ),
    FOREIGN KEY (objective_id, objective_version)
        REFERENCES objective_contracts(objective_id, version),
    FOREIGN KEY (plan_id, plan_version)
        REFERENCES plan_graphs(plan_id, version),
    FOREIGN KEY (provider_id, provider_profile_version)
        REFERENCES provider_registry(provider_id, profile_version),
    FOREIGN KEY (authorization_id)
        REFERENCES authorization_artifacts(authorization_id)
);

CREATE TABLE cognitive_run_checkpoints (
    run_id TEXT NOT NULL REFERENCES cognitive_runs(run_id),
    checkpoint_sequence INTEGER NOT NULL CHECK (checkpoint_sequence >= 1),
    safe_to_resume INTEGER NOT NULL CHECK (safe_to_resume IN (0, 1)),
    authorization_digest TEXT,
    last_completed_execution_id TEXT,
    canonical_checkpoint TEXT NOT NULL CHECK (json_valid(canonical_checkpoint)),
    checkpoint_digest TEXT NOT NULL UNIQUE CHECK (
        length(checkpoint_digest) = 64
    ),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, checkpoint_sequence)
);

CREATE TABLE tool_proposals (
    proposal_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES cognitive_runs(run_id),
    objective_id TEXT NOT NULL,
    objective_digest TEXT NOT NULL CHECK (length(objective_digest) = 64),
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64),
    plan_node_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    capability_version INTEGER NOT NULL,
    capability_digest TEXT NOT NULL CHECK (length(capability_digest) = 64),
    authorization_id TEXT NOT NULL
        REFERENCES authorization_artifacts(authorization_id),
    authorization_digest TEXT NOT NULL CHECK (
        length(authorization_digest) = 64
    ),
    risk_class TEXT NOT NULL CHECK (
        risk_class IN ('low', 'medium', 'high', 'critical')
    ),
    canonical_typed_arguments TEXT NOT NULL CHECK (
        json_valid(canonical_typed_arguments)
    ),
    argument_digest TEXT NOT NULL CHECK (length(argument_digest) = 64),
    idempotency_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    expected_observation TEXT NOT NULL,
    runtime_execution_reference TEXT,
    runtime_receipt_digest TEXT CHECK (
        runtime_receipt_digest IS NULL OR length(runtime_receipt_digest) = 64
    ),
    status TEXT NOT NULL CHECK (status = 'proposed'),
    proposed_at TEXT NOT NULL,
    canonical_proposal TEXT NOT NULL CHECK (json_valid(canonical_proposal)),
    proposal_digest TEXT NOT NULL UNIQUE CHECK (length(proposal_digest) = 64),
    UNIQUE (proposal_id, run_id),
    FOREIGN KEY (plan_id, plan_version, plan_node_id)
        REFERENCES plan_nodes(plan_id, plan_version, node_id),
    FOREIGN KEY (capability_id, capability_version, capability_digest)
        REFERENCES capability_registry(
            capability_id, capability_version, capability_digest
        )
);

CREATE TABLE tool_executions (
    execution_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES tool_proposals(proposal_id),
    run_id TEXT NOT NULL REFERENCES cognitive_runs(run_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    reservation_event_id TEXT UNIQUE REFERENCES state_events(event_id),
    reservation_receipt_digest TEXT CHECK (
        reservation_receipt_digest IS NULL
        OR length(reservation_receipt_digest) = 64
    ),
    preview_id TEXT,
    preview_digest TEXT CHECK (
        preview_digest IS NULL OR length(preview_digest) = 64
    ),
    attempt_number INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 2),
    policy_decision TEXT NOT NULL CHECK (
        policy_decision IN (
            'allow', 'deny_schema', 'deny_scope', 'deny_authority',
            'deny_risk', 'deny_path', 'deny_command', 'deny_secret',
            'deny_budget', 'deny_replay'
        )
    ),
    resolved_argv_json TEXT NOT NULL CHECK (
        json_valid(resolved_argv_json)
        AND json_type(resolved_argv_json) = 'array'
    ),
    invocation_kind TEXT NOT NULL CHECK (
        invocation_kind IN ('native_call', 'fixed_process', 'denied')
    ),
    executor_kind TEXT NOT NULL CHECK (
        executor_kind IN (
            'native', 'runtime', 'codex', 'external_agent', 'local_broker'
        )
    ),
    reason_codes_json TEXT NOT NULL CHECK (
        json_valid(reason_codes_json)
        AND json_type(reason_codes_json) = 'array'
        AND json_array_length(reason_codes_json) >= 1
    ),
    working_directory TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'denied', 'succeeded', 'failed', 'cancelled',
            'recovery_required'
        )
    ),
    started_at TEXT,
    finished_at TEXT NOT NULL,
    exit_code INTEGER,
    stdout_digest TEXT,
    stderr_digest TEXT,
    runtime_execution_reference TEXT,
    runtime_receipt_digest TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('pending', 'passed', 'failed', 'unverifiable')
    ),
    canonical_execution TEXT NOT NULL CHECK (json_valid(canonical_execution)),
    execution_digest TEXT NOT NULL UNIQUE CHECK (length(execution_digest) = 64),
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (proposal_id, run_id)
        REFERENCES tool_proposals(proposal_id, run_id),
    CHECK (
        (
            policy_decision = 'allow'
            AND reservation_event_id IS NOT NULL
            AND reservation_receipt_digest IS NOT NULL
            AND preview_id IS NOT NULL
            AND length(preview_id) >= 1
            AND preview_digest IS NOT NULL
            AND (
                (
                    invocation_kind = 'native_call'
                    AND json_array_length(resolved_argv_json) = 0
                )
                OR
                (
                    invocation_kind = 'fixed_process'
                    AND json_array_length(resolved_argv_json) >= 1
                )
            )
            AND working_directory IS NOT NULL
            AND length(working_directory) >= 1
            AND status IN (
                'succeeded', 'failed', 'cancelled', 'recovery_required'
            )
            AND started_at IS NOT NULL
        )
        OR
        (
            policy_decision <> 'allow'
            AND reservation_event_id IS NULL
            AND reservation_receipt_digest IS NULL
            AND preview_id IS NULL
            AND preview_digest IS NULL
            AND invocation_kind = 'denied'
            AND json_array_length(resolved_argv_json) = 0
            AND working_directory IS NULL
            AND status = 'denied'
            AND started_at IS NULL
            AND exit_code IS NULL
        )
    )
);

CREATE TRIGGER objective_contracts_no_update
BEFORE UPDATE ON objective_contracts
BEGIN
    SELECT RAISE(ABORT, 'objective_contracts_append_only');
END;

CREATE TRIGGER objective_contracts_no_delete
BEFORE DELETE ON objective_contracts
BEGIN
    SELECT RAISE(ABORT, 'objective_contracts_append_only');
END;

CREATE TRIGGER capability_registry_no_update
BEFORE UPDATE ON capability_registry
BEGIN
    SELECT RAISE(ABORT, 'capability_registry_append_only');
END;

CREATE TRIGGER capability_registry_no_delete
BEFORE DELETE ON capability_registry
BEGIN
    SELECT RAISE(ABORT, 'capability_registry_append_only');
END;

CREATE TRIGGER plan_graphs_no_update
BEFORE UPDATE ON plan_graphs
BEGIN
    SELECT RAISE(ABORT, 'plan_graphs_append_only');
END;

CREATE TRIGGER plan_graphs_no_delete
BEFORE DELETE ON plan_graphs
BEGIN
    SELECT RAISE(ABORT, 'plan_graphs_append_only');
END;

CREATE TRIGGER plan_nodes_no_update
BEFORE UPDATE ON plan_nodes
BEGIN
    SELECT RAISE(ABORT, 'plan_nodes_append_only');
END;

CREATE TRIGGER plan_nodes_no_delete
BEFORE DELETE ON plan_nodes
BEGIN
    SELECT RAISE(ABORT, 'plan_nodes_append_only');
END;

CREATE TRIGGER plan_edges_no_update
BEFORE UPDATE ON plan_edges
BEGIN
    SELECT RAISE(ABORT, 'plan_edges_append_only');
END;

CREATE TRIGGER plan_edges_no_delete
BEFORE DELETE ON plan_edges
BEGIN
    SELECT RAISE(ABORT, 'plan_edges_append_only');
END;

CREATE TRIGGER plan_revisions_no_update
BEFORE UPDATE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan_revisions_append_only');
END;

CREATE TRIGGER plan_revisions_no_delete
BEFORE DELETE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'plan_revisions_append_only');
END;

CREATE TRIGGER authorization_artifacts_no_update
BEFORE UPDATE ON authorization_artifacts
BEGIN
    SELECT RAISE(ABORT, 'authorization_artifacts_append_only');
END;

CREATE TRIGGER authorization_artifacts_no_delete
BEFORE DELETE ON authorization_artifacts
BEGIN
    SELECT RAISE(ABORT, 'authorization_artifacts_append_only');
END;

CREATE TRIGGER context_bindings_no_update
BEFORE UPDATE ON context_bindings
BEGIN
    SELECT RAISE(ABORT, 'context_bindings_append_only');
END;

CREATE TRIGGER context_bindings_no_delete
BEFORE DELETE ON context_bindings
BEGIN
    SELECT RAISE(ABORT, 'context_bindings_append_only');
END;

CREATE TRIGGER cognitive_run_checkpoints_no_update
BEFORE UPDATE ON cognitive_run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'cognitive_run_checkpoints_append_only');
END;

CREATE TRIGGER cognitive_run_checkpoints_no_delete
BEFORE DELETE ON cognitive_run_checkpoints
BEGIN
    SELECT RAISE(ABORT, 'cognitive_run_checkpoints_append_only');
END;

CREATE TRIGGER tool_proposals_no_update
BEFORE UPDATE ON tool_proposals
BEGIN
    SELECT RAISE(ABORT, 'tool_proposals_append_only');
END;

CREATE TRIGGER tool_proposals_no_delete
BEFORE DELETE ON tool_proposals
BEGIN
    SELECT RAISE(ABORT, 'tool_proposals_append_only');
END;

CREATE TRIGGER tool_executions_no_update
BEFORE UPDATE ON tool_executions
BEGIN
    SELECT RAISE(ABORT, 'tool_executions_append_only');
END;

CREATE TRIGGER tool_executions_no_delete
BEFORE DELETE ON tool_executions
BEGIN
    SELECT RAISE(ABORT, 'tool_executions_append_only');
END;

CREATE INDEX objective_contracts_project_order
ON objective_contracts(project_id, objective_id, version);

CREATE INDEX plan_graphs_objective_order
ON plan_graphs(objective_id, objective_version, plan_id, version);

CREATE INDEX plan_edges_outgoing
ON plan_edges(plan_id, plan_version, from_node_id, priority, edge_id);

CREATE INDEX context_bindings_objective_source
ON context_bindings(objective_id, objective_version, source_system, binding_id);

CREATE INDEX cognitive_runs_status_activity
ON cognitive_runs(status, last_activity_at, run_id);

CREATE INDEX tool_proposals_run_status
ON tool_proposals(run_id, status, proposed_at, proposal_id);

CREATE INDEX tool_executions_run_status
ON tool_executions(run_id, status, recorded_at, execution_id);
