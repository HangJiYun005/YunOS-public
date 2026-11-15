"""Sealed review-authority boundary for YunOS v6.1 relation review."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from memory.db_path_policy import (
    DEFAULT_KNOWLEDGE_SIDECAR_PATH,
    DEFAULT_MEMORY_SIDECAR_PATH,
    DEFAULT_REAL_DB_PATH,
    DEFAULT_SEMANTIC_SIDECAR_PATH,
    is_reparse_point,
    validate_knowledge_test_db_target,
)
from memory.graph_identity import canonical_hash
from memory.relation_review import (
    AUTHORITY_PROTOCOL_IDENTITY,
    AUTHORITY_SCHEMA_VERSION,
    REVIEW_POLICY_IDENTITY,
    DecisionEnvelope,
    RelationCandidate,
    RelationReviewError,
    ReviewPlan,
    SupersessionRecord,
    validate_candidate_identity,
    validate_decision_identity,
    validate_review_plan_identity,
    validate_supersession_identity,
    required_confirmation_fields,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_AUTHORITY_ROOT = PROJECT_ROOT / "data" / "review"
PRODUCTION_REGISTRY_PATH = PRODUCTION_AUTHORITY_ROOT / "ayunos_review_authority_registry.db"
PRODUCTION_LEDGER_PATH = PRODUCTION_AUTHORITY_ROOT / "ayunos_review_authority.db"
REGISTRY_NAME = "authority-registry.db"
LEDGER_NAME = "authority-ledger.db"

def _build_handle_capability_runtime():
    issued = weakref.WeakSet()
    authorized_issuer_codes: set[Any] = set()

    def issue(registry_path: Path, ledger_path: Path, identity: Any):
        if sys._getframe(1).f_code not in authorized_issuer_codes:
            raise RelationReviewError("review authority handles are issued only by an authority factory")
        handle = object.__new__(ReviewAuthorityHandle)
        handle._ReviewAuthorityHandle__registry_path = registry_path
        handle._ReviewAuthorityHandle__ledger_path = ledger_path
        handle._ReviewAuthorityHandle__identity = identity
        issued.add(handle)
        return handle

    def is_trusted(value: Any) -> bool:
        return isinstance(value, ReviewAuthorityHandle) and value in issued

    def authorize(code: Any) -> None:
        authorized_issuer_codes.add(code)

    return issue, is_trusted, authorize


_issue_authority_handle, is_trusted_review_authority_handle, _authorize_handle_issuer = _build_handle_capability_runtime()


class ReviewAuthorityNotInitialized(RelationReviewError):
    pass


class ReviewAuthorityStateLost(RelationReviewError):
    pass


class ReviewAuthorityMismatch(RelationReviewError):
    pass


REGISTRY_SQL = """
CREATE TABLE authority_registry_metadata(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 authority_store_id TEXT NOT NULL UNIQUE,
 authority_schema_version TEXT NOT NULL,
 authority_generation TEXT NOT NULL,
 ledger_instance_id TEXT NOT NULL UNIQUE,
 initialization_seal TEXT NOT NULL UNIQUE,
 authority_metadata_digest TEXT NOT NULL UNIQUE,
 authority_protocol_identity TEXT NOT NULL,
 registry_schema_fingerprint TEXT NOT NULL,
 ledger_schema_fingerprint TEXT NOT NULL
);
CREATE TABLE authority_registry_state(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 mutation_seq INTEGER NOT NULL CHECK(mutation_seq>=0),
 mutation_digest TEXT NOT NULL
);
CREATE TRIGGER authority_registry_metadata_no_update BEFORE UPDATE ON authority_registry_metadata BEGIN SELECT RAISE(ABORT,'immutable authority metadata'); END;
CREATE TRIGGER authority_registry_metadata_no_delete BEFORE DELETE ON authority_registry_metadata BEGIN SELECT RAISE(ABORT,'immutable authority metadata'); END;
CREATE TRIGGER authority_registry_state_no_delete BEFORE DELETE ON authority_registry_state BEGIN SELECT RAISE(ABORT,'authority state cannot be deleted'); END;
CREATE TRIGGER authority_registry_state_no_insert BEFORE INSERT ON authority_registry_state WHEN EXISTS(SELECT 1 FROM authority_registry_state) BEGIN SELECT RAISE(ABORT,'authority state singleton'); END;
"""


LEDGER_SQL = """
CREATE TABLE authority_ledger_metadata(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 authority_store_id TEXT NOT NULL UNIQUE,
 authority_schema_version TEXT NOT NULL,
 authority_generation TEXT NOT NULL,
 ledger_instance_id TEXT NOT NULL UNIQUE,
 initialization_seal TEXT NOT NULL UNIQUE,
 authority_metadata_digest TEXT NOT NULL UNIQUE,
 authority_protocol_identity TEXT NOT NULL,
 registry_schema_fingerprint TEXT NOT NULL,
 ledger_schema_fingerprint TEXT NOT NULL,
 mutation_seq INTEGER NOT NULL CHECK(mutation_seq>=0),
 mutation_digest TEXT NOT NULL
);
CREATE TABLE review_candidates(
 candidate_id TEXT PRIMARY KEY,
 candidate_schema_version TEXT NOT NULL,
 candidate_payload_hash TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL
);
CREATE TABLE review_chains(
 review_chain_id TEXT PRIMARY KEY,
 chain_key TEXT NOT NULL UNIQUE,
 family_identity TEXT NOT NULL,
 candidate_id TEXT NOT NULL UNIQUE REFERENCES review_candidates(candidate_id),
 candidate_payload_hash TEXT NOT NULL,
 review_policy_identity TEXT NOT NULL,
 predecessor_review_chain_id TEXT UNIQUE REFERENCES review_chains(review_chain_id)
);
CREATE INDEX review_chains_family_idx ON review_chains(family_identity);
CREATE TABLE review_chain_predecessors(
 successor_review_chain_id TEXT NOT NULL REFERENCES review_chains(review_chain_id),
 predecessor_review_chain_id TEXT NOT NULL UNIQUE REFERENCES review_chains(review_chain_id),
 PRIMARY KEY(successor_review_chain_id,predecessor_review_chain_id),
 CHECK(successor_review_chain_id<>predecessor_review_chain_id)
);
CREATE INDEX review_chain_predecessors_successor_idx ON review_chain_predecessors(successor_review_chain_id);
CREATE TABLE review_sessions(
 review_session_id TEXT PRIMARY KEY,
 review_chain_id TEXT NOT NULL UNIQUE REFERENCES review_chains(review_chain_id),
 authority_store_id TEXT NOT NULL
);
CREATE TABLE review_plans(
 review_plan_id TEXT PRIMARY KEY,
 review_schema_version TEXT NOT NULL,
 review_chain_id TEXT NOT NULL UNIQUE REFERENCES review_chains(review_chain_id),
 review_session_id TEXT NOT NULL UNIQUE REFERENCES review_sessions(review_session_id),
 candidate_id TEXT NOT NULL UNIQUE REFERENCES review_candidates(candidate_id),
 plan_payload_hash TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL
);
CREATE TABLE review_decisions(
 decision_id TEXT PRIMARY KEY,
 decision_schema_version TEXT NOT NULL,
 review_plan_id TEXT NOT NULL UNIQUE REFERENCES review_plans(review_plan_id),
 review_chain_id TEXT NOT NULL UNIQUE REFERENCES review_chains(review_chain_id),
 decision_payload_hash TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL
);
CREATE TABLE review_supersessions(
 supersession_id TEXT PRIMARY KEY,
 supersession_schema_version TEXT NOT NULL,
 decision_id TEXT NOT NULL UNIQUE REFERENCES review_decisions(decision_id),
 original_review_chain_id TEXT NOT NULL UNIQUE REFERENCES review_chains(review_chain_id),
 original_candidate_id TEXT NOT NULL UNIQUE REFERENCES review_candidates(candidate_id),
 replacement_review_chain_id TEXT NOT NULL REFERENCES review_chains(review_chain_id),
 replacement_candidate_id TEXT NOT NULL REFERENCES review_candidates(candidate_id),
 payload_json TEXT NOT NULL,
 CHECK(original_review_chain_id<>replacement_review_chain_id),
 CHECK(original_candidate_id<>replacement_candidate_id)
);
CREATE TABLE authority_events(
 mutation_seq INTEGER PRIMARY KEY CHECK(mutation_seq>0),
 previous_digest TEXT NOT NULL,
 mutation_digest TEXT NOT NULL UNIQUE,
 event_kind TEXT NOT NULL,
 event_identity TEXT NOT NULL,
 recorded_at TEXT NOT NULL
);
CREATE TRIGGER authority_ledger_metadata_identity_immutable BEFORE UPDATE ON authority_ledger_metadata WHEN NEW.authority_store_id<>OLD.authority_store_id OR NEW.authority_schema_version<>OLD.authority_schema_version OR NEW.authority_generation<>OLD.authority_generation OR NEW.ledger_instance_id<>OLD.ledger_instance_id OR NEW.initialization_seal<>OLD.initialization_seal OR NEW.authority_metadata_digest<>OLD.authority_metadata_digest OR NEW.authority_protocol_identity<>OLD.authority_protocol_identity OR NEW.registry_schema_fingerprint<>OLD.registry_schema_fingerprint OR NEW.ledger_schema_fingerprint<>OLD.ledger_schema_fingerprint BEGIN SELECT RAISE(ABORT,'immutable authority identity'); END;
CREATE TRIGGER authority_ledger_metadata_no_delete BEFORE DELETE ON authority_ledger_metadata BEGIN SELECT RAISE(ABORT,'authority metadata cannot be deleted'); END;
CREATE TRIGGER review_candidates_no_update BEFORE UPDATE ON review_candidates BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_candidates_no_delete BEFORE DELETE ON review_candidates BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_chains_no_update BEFORE UPDATE ON review_chains BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_chains_no_delete BEFORE DELETE ON review_chains BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_chain_predecessors_no_update BEFORE UPDATE ON review_chain_predecessors BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_chain_predecessors_no_delete BEFORE DELETE ON review_chain_predecessors BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_sessions_no_update BEFORE UPDATE ON review_sessions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_sessions_no_delete BEFORE DELETE ON review_sessions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_plans_no_update BEFORE UPDATE ON review_plans BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_plans_no_delete BEFORE DELETE ON review_plans BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_decisions_no_update BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_decisions_no_delete BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_supersessions_no_update BEFORE UPDATE ON review_supersessions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER review_supersessions_no_delete BEFORE DELETE ON review_supersessions BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER authority_events_no_update BEFORE UPDATE ON authority_events BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER authority_events_no_delete BEFORE DELETE ON authority_events BEGIN SELECT RAISE(ABORT,'append only'); END;
"""


def _schema_fingerprint(script: str) -> str:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name").fetchall()
    finally:
        conn.close()
    return canonical_hash("review_authority_schema", {"objects": [[a, b, " ".join(c.split())] for a, b, c in rows]})


REGISTRY_SCHEMA_FINGERPRINT = _schema_fingerprint(REGISTRY_SQL)
LEDGER_SCHEMA_FINGERPRINT = _schema_fingerprint(LEDGER_SQL)


@dataclass(frozen=True)
class ReviewAuthorityIdentity:
    authority_store_id: str
    authority_schema_version: str
    authority_generation: str
    ledger_instance_id: str
    initialization_seal: str
    authority_metadata_digest: str
    authority_protocol_identity: str
    registry_schema_fingerprint: str
    ledger_schema_fingerprint: str

    def public_context(self) -> dict[str, str]:
        return {
            "authority_store_id": self.authority_store_id,
            "authority_schema_version": self.authority_schema_version,
            "authority_generation": self.authority_generation,
            "authority_metadata_digest": self.authority_metadata_digest,
            "authority_protocol_identity": self.authority_protocol_identity,
        }


class ReviewAuthorityHandle:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RelationReviewError("review authority handles are issued only by an authority factory")

    @property
    def identity(self) -> ReviewAuthorityIdentity:
        self.assert_trusted()
        return self.__identity

    def assert_trusted(self) -> None:
        if not is_trusted_review_authority_handle(self):
            raise RelationReviewError("review authority handle is not trusted")
        current, registry, ledger = _open_and_validate(self.__registry_path, self.__ledger_path)
        registry.close(); ledger.close()
        if current != self.__identity:
            raise ReviewAuthorityMismatch("review authority identity changed")

    def assert_plan_identity(self, plan: ReviewPlan) -> None:
        self.assert_trusted()
        identity = self.__identity
        expected = (
            identity.authority_schema_version, identity.authority_store_id,
            identity.authority_generation, identity.authority_metadata_digest,
            identity.authority_protocol_identity,
        )
        actual = (
            plan.authority_schema_version, plan.authority_store_id,
            plan.authority_generation, plan.authority_metadata_digest,
            plan.authority_protocol_identity,
        )
        if actual != expected:
            raise ReviewAuthorityMismatch("review plan belongs to another authority")

    def current_persistence_record(
        self, candidate_id: str, review_plan_id: str, decision_id: str,
    ) -> dict[str, Any]:
        """Load a persistence handoff from the validated authority, never the client."""
        self.assert_trusted()
        identity, registry, ledger = _open_and_validate(self.__registry_path, self.__ledger_path)
        registry.close()
        try:
            return _persistence_record_from_ledger(identity, ledger, candidate_id, review_plan_id, decision_id)
        finally:
            ledger.close()

    def reserve_persistence(
        self, candidate_id: str, review_plan_id: str, decision_id: str,
    ) -> "_AuthorityPersistenceReservation":
        """Hold the registry/ledger mutation boundary until knowledge commit."""
        self.assert_trusted()
        return _AuthorityPersistenceReservation(
            self.__registry_path, self.__ledger_path, self.__identity,
            candidate_id, review_plan_id, decision_id,
        )

    def _prepare_plan(self, candidate: RelationCandidate, builder: Callable[[dict[str, Any]], ReviewPlan]) -> dict[str, Any]:
        validate_candidate_identity(candidate)
        chain_key = canonical_hash("review_chain_key", {
            "authority_store_id": self.__identity.authority_store_id,
            "candidate_id": candidate.candidate_id,
            "candidate_payload_hash": candidate.payload_hash,
            "review_policy_identity": REVIEW_POLICY_IDENTITY,
        })
        family = canonical_hash("review_chain_family", {
            "relation_type": candidate.relation_type,
            "direction": candidate.direction,
            "source_node_id": candidate.source_node_id,
            "target_node_id": candidate.target_node_id,
            "review_policy_identity": REVIEW_POLICY_IDENTITY,
        })

        def mutate(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any] | None]:
            row = conn.execute("SELECT review_chain_id FROM review_chains WHERE chain_key=?", (chain_key,)).fetchone()
            if row:
                plan = _load_plan_for_chain(conn, row[0])
                return {"status": _chain_status(conn, row[0]), "plan": plan, "next_action": _chain_next_action(conn, row[0])}, None
            predecessors = conn.execute(
                "SELECT c.review_chain_id,k.payload_json FROM review_chains c "
                "JOIN review_candidates k ON k.candidate_id=c.candidate_id "
                "WHERE c.family_identity=? ORDER BY c.rowid", (family,)
            ).fetchall()
            # Alternatives discovered in one immutable graph build may be reviewed
            # together (and are the only valid replacement targets).  A later graph
            # build is a successor and makes the previous family chain stale.
            stale_predecessors = sorted(
                row[0] for row in predecessors
                if json.loads(row[1])["graph_build_identity"] != candidate.graph_build_identity
                and _chain_status(conn, row[0]) == "ready"
            )
            predecessor_id = stale_predecessors[0] if stale_predecessors else None
            chain_id = "chain_" + canonical_hash("review_chain", {"authority_store_id": self.__identity.authority_store_id, "chain_key": chain_key})
            session_id = "session_" + secrets.token_hex(32)
            context = {**self.__identity.public_context(), "review_session_id": session_id, "review_chain_id": chain_id, "predecessor_review_chain_id": predecessor_id}
            plan = builder(context)
            validate_review_plan_identity(plan)
            _validate_candidate_plan(candidate, plan, self.__identity)
            conn.execute("INSERT INTO review_candidates VALUES(?,?,?,?)", (candidate.candidate_id, candidate.candidate_schema_version, candidate.payload_hash, _json(candidate.public_dict())))
            conn.execute("INSERT INTO review_chains VALUES(?,?,?,?,?,?,?)", (chain_id, chain_key, family, candidate.candidate_id, candidate.payload_hash, REVIEW_POLICY_IDENTITY, predecessor_id))
            conn.execute("INSERT INTO review_sessions VALUES(?,?,?)", (session_id, chain_id, self.__identity.authority_store_id))
            conn.execute("INSERT INTO review_plans VALUES(?,?,?,?,?,?,?)", (plan.review_plan_id, plan.review_schema_version, chain_id, session_id, candidate.candidate_id, plan.payload_hash, _json(plan.public_dict())))
            conn.executemany("INSERT INTO review_chain_predecessors VALUES(?,?)", ((chain_id, old_id) for old_id in stale_predecessors))
            return {"status": "ready", "plan": plan, "next_action": "decide"}, {"event_kind": "plan_prepared", "event_identity": _plan_event_identity(plan.review_plan_id, stale_predecessors)}

        return self.__mutate(mutate)

    def _inspect_plan(self, plan: ReviewPlan) -> dict[str, Any]:
        self.assert_plan_identity(plan)
        validate_review_plan_identity(plan)
        identity, registry, ledger = _open_and_validate(self.__registry_path, self.__ledger_path)
        registry.close()
        try:
            row = ledger.execute("SELECT review_chain_id,review_session_id,payload_json FROM review_plans WHERE review_plan_id=?", (plan.review_plan_id,)).fetchone()
            if row is None or row != (plan.review_chain_id, plan.review_session_id, _json(plan.public_dict())):
                return _state("stale", "plan_not_registered_in_authority", "prepare_again")
            return _inspect_chain(ledger, plan.review_chain_id)
        finally:
            ledger.close()

    def _commit_decision(self, candidate: RelationCandidate, plan: ReviewPlan, envelope: DecisionEnvelope, *, replacement_candidate: RelationCandidate | None, replacement_plan: ReviewPlan | None, supersession: SupersessionRecord | None) -> dict[str, Any]:
        self.assert_plan_identity(plan)

        def mutate(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any] | None]:
            _validate_candidate_plan(candidate, plan, self.__identity)
            validate_decision_identity(envelope)
            _validate_decision_protocol(envelope, plan, self.__identity)
            registered = conn.execute("SELECT payload_json FROM review_plans WHERE review_plan_id=? AND review_chain_id=? AND review_session_id=?", (plan.review_plan_id, plan.review_chain_id, plan.review_session_id)).fetchone()
            if registered is None or registered[0] != _json(plan.public_dict()):
                return _state("stale", "plan_not_registered_in_authority", "prepare_again"), None
            prior = conn.execute("SELECT decision_id,decision_payload_hash,payload_json FROM review_decisions WHERE review_plan_id=?", (plan.review_plan_id,)).fetchone()
            if prior:
                if prior[0] == envelope.decision_id and prior[1] == envelope.payload_hash:
                    return {"status": "idempotent", "envelope": _envelope(prior[2])}, None
                if conn.execute("SELECT 1 FROM review_supersessions WHERE original_review_chain_id=?", (plan.review_chain_id,)).fetchone():
                    return _state("superseded", "plan_superseded", "review_replacement"), None
                return _state("decision_conflict", "different_decision_already_recorded", "manual_review"), None
            chain_state = _chain_status(conn, plan.review_chain_id)
            if chain_state != "ready":
                return _state(chain_state, "review_chain_not_active", "manual_review"), None
            if envelope.decision == "replace":
                if replacement_candidate is None or replacement_plan is None or supersession is None:
                    raise RelationReviewError("replacement protocol is incomplete")
                _validate_candidate_plan(replacement_candidate, replacement_plan, self.__identity)
                validate_supersession_identity(supersession)
                _validate_supersession_protocol(supersession, envelope, plan, replacement_plan, self.__identity, candidate, replacement_candidate)
                replacement_state = _inspect_chain(conn, replacement_plan.review_chain_id)
                if replacement_state["status"] != "valid":
                    return _state(replacement_state["status"], "replacement_chain_not_active", "manual_review"), None
            conn.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?)", (envelope.decision_id, envelope.decision_schema_version, plan.review_plan_id, plan.review_chain_id, envelope.payload_hash, _json(envelope.public_dict())))
            if supersession is not None and replacement_candidate is not None and replacement_plan is not None:
                conn.execute("INSERT INTO review_supersessions VALUES(?,?,?,?,?,?,?,?)", (supersession.supersession_id, supersession.supersession_schema_version, envelope.decision_id, plan.review_chain_id, candidate.candidate_id, replacement_plan.review_chain_id, replacement_candidate.candidate_id, _json(supersession.public_dict())))
            return {"status": "recorded", "envelope": envelope}, {"event_kind": "decision_recorded", "event_identity": _decision_event_identity(envelope.decision_id, supersession.supersession_id if supersession else None)}

        try:
            return self.__mutate(mutate)
        except sqlite3.IntegrityError:
            return _state("decision_conflict", "concurrent_decision_conflict", "manual_review")

    def __mutate(self, mutator: Callable[[sqlite3.Connection], tuple[dict[str, Any], dict[str, Any] | None]]) -> dict[str, Any]:
        self.assert_trusted()
        registry = sqlite3.connect(self.__registry_path, timeout=30)
        ledger = sqlite3.connect(self.__ledger_path, timeout=30)
        ledger_committed = False
        try:
            registry.execute("BEGIN IMMEDIATE")
            ledger.execute("PRAGMA foreign_keys=ON")
            ledger.execute("BEGIN IMMEDIATE")
            _validate_pair_connections(registry, ledger, self.__identity)
            result, event = mutator(ledger)
            if event is None:
                ledger.rollback(); registry.rollback(); return result
            seq, previous = registry.execute("SELECT mutation_seq,mutation_digest FROM authority_registry_state WHERE singleton=1").fetchone()
            next_seq = int(seq) + 1
            digest = canonical_hash("review_authority_mutation", {"authority_store_id": self.__identity.authority_store_id, "mutation_seq": next_seq, "previous_digest": previous, **event})
            ledger.execute("INSERT INTO authority_events VALUES(?,?,?,?,?,?)", (next_seq, previous, digest, event["event_kind"], event["event_identity"], _utc_now()))
            ledger.execute("UPDATE authority_ledger_metadata SET mutation_seq=?,mutation_digest=? WHERE singleton=1", (next_seq, digest))
            ledger.commit(); ledger_committed = True
            registry.execute("UPDATE authority_registry_state SET mutation_seq=?,mutation_digest=? WHERE singleton=1", (next_seq, digest))
            registry.commit()
            return result
        except Exception:
            if ledger.in_transaction: ledger.rollback()
            if registry.in_transaction: registry.rollback()
            if ledger_committed:
                raise ReviewAuthorityStateLost("authority mutation seal update was interrupted")
            raise
        finally:
            ledger.close(); registry.close()


class _AuthorityPersistenceReservation:
    """Read validated persistence facts while excluding Authority mutations."""

    def __init__(self, registry_path: Path, ledger_path: Path, identity: ReviewAuthorityIdentity,
                 candidate_id: str, review_plan_id: str, decision_id: str) -> None:
        self.registry_path = registry_path; self.ledger_path = ledger_path
        self.identity = identity; self.ids = (candidate_id, review_plan_id, decision_id)
        self.registry: sqlite3.Connection | None = None
        self.ledger: sqlite3.Connection | None = None

    def __enter__(self) -> dict[str, Any]:
        if self.registry is not None or self.ledger is not None:
            raise ReviewAuthorityMismatch("persistence reservation cannot be reused")
        registry = sqlite3.connect(self.registry_path, timeout=30)
        ledger = sqlite3.connect(self.ledger_path, timeout=30)
        try:
            registry.execute("BEGIN IMMEDIATE")
            ledger.execute("PRAGMA foreign_keys=ON")
            ledger.execute("BEGIN IMMEDIATE")
            _validate_pair_connections(registry, ledger, self.identity)
            facts = _persistence_record_from_ledger(self.identity, ledger, *self.ids)
            self.registry = registry; self.ledger = ledger
            return facts
        except Exception:
            if ledger.in_transaction: ledger.rollback()
            if registry.in_transaction: registry.rollback()
            ledger.close(); registry.close()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        ledger, registry = self.ledger, self.registry
        self.ledger = None; self.registry = None
        try:
            if ledger is not None and ledger.in_transaction: ledger.rollback()
            if registry is not None and registry.in_transaction: registry.rollback()
        finally:
            if ledger is not None: ledger.close()
            if registry is not None: registry.close()


class ProductionReviewAuthorityFactory:
    @staticmethod
    def open() -> ReviewAuthorityHandle:
        if not PRODUCTION_AUTHORITY_ROOT.is_dir() or not PRODUCTION_REGISTRY_PATH.is_file() or not PRODUCTION_LEDGER_PATH.is_file():
            raise ReviewAuthorityNotInitialized("production review authority is not initialized")
        _validate_production_paths()
        identity, registry, ledger = _open_and_validate(PRODUCTION_REGISTRY_PATH, PRODUCTION_LEDGER_PATH)
        registry.close(); ledger.close()
        return _issue_authority_handle(PRODUCTION_REGISTRY_PATH, PRODUCTION_LEDGER_PATH, identity)


class _TestReviewAuthorityFactory:
    """Explicit test-only authority replicas restricted to the OS temporary root."""

    @staticmethod
    def initialize(root: str | Path) -> ReviewAuthorityHandle:
        registry_path, ledger_path = _validated_test_paths(root)
        if registry_path.exists() or ledger_path.exists():
            raise RelationReviewError("test review authority cannot be reinitialized")
        identity = _new_identity()
        initial_digest = canonical_hash("review_authority_initial_state", asdict(identity))
        created: list[Path] = []
        try:
            registry = sqlite3.connect(registry_path); created.append(registry_path)
            try:
                registry.executescript(REGISTRY_SQL)
                registry.execute("INSERT INTO authority_registry_metadata VALUES(1,?,?,?,?,?,?,?,?,?)", tuple(asdict(identity).values()))
                registry.execute("INSERT INTO authority_registry_state VALUES(1,0,?)", (initial_digest,))
                registry.commit()
            finally: registry.close()
            ledger = sqlite3.connect(ledger_path); created.append(ledger_path)
            try:
                ledger.execute("PRAGMA foreign_keys=ON"); ledger.executescript(LEDGER_SQL)
                ledger.execute("INSERT INTO authority_ledger_metadata VALUES(1,?,?,?,?,?,?,?,?,?,?,?)", (*tuple(asdict(identity).values()), 0, initial_digest))
                ledger.commit()
            finally: ledger.close()
            opened = _TestReviewAuthorityFactory.open(root)
            return opened
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def open(root: str | Path) -> ReviewAuthorityHandle:
        registry_path, ledger_path = _validated_test_paths(root)
        if not registry_path.is_file() or not ledger_path.is_file():
            if registry_path.exists() or ledger_path.exists():
                raise ReviewAuthorityStateLost("test review authority registry or ledger is missing")
            raise ReviewAuthorityNotInitialized("test review authority is not initialized")
        identity, registry, ledger = _open_and_validate(registry_path, ledger_path)
        registry.close(); ledger.close()
        return _issue_authority_handle(registry_path, ledger_path, identity)


_authorize_handle_issuer(ProductionReviewAuthorityFactory.open.__code__)
_authorize_handle_issuer(_TestReviewAuthorityFactory.open.__code__)
del _authorize_handle_issuer


def _new_identity() -> ReviewAuthorityIdentity:
    base = {
        "authority_store_id": "authority_" + secrets.token_hex(32),
        "authority_schema_version": AUTHORITY_SCHEMA_VERSION,
        "authority_generation": "generation_" + secrets.token_hex(32),
        "ledger_instance_id": "ledger_" + secrets.token_hex(32),
        "initialization_seal": "seal_" + secrets.token_hex(32),
        "authority_protocol_identity": AUTHORITY_PROTOCOL_IDENTITY,
        "registry_schema_fingerprint": REGISTRY_SCHEMA_FINGERPRINT,
        "ledger_schema_fingerprint": LEDGER_SCHEMA_FINGERPRINT,
    }
    digest = canonical_hash("review_authority_metadata", base)
    return ReviewAuthorityIdentity(authority_metadata_digest=digest, **base)


def _validated_test_paths(root: str | Path) -> tuple[Path, Path]:
    directory = Path(root).expanduser().absolute()
    temp_root = Path(tempfile.gettempdir()).absolute()
    try: directory.relative_to(temp_root)
    except ValueError as exc: raise RelationReviewError("test review authority requires the system temporary directory") from exc
    if directory == temp_root or not directory.is_dir() or is_reparse_point(directory):
        raise RelationReviewError("test review authority root is invalid")
    registry, ledger = directory / REGISTRY_NAME, directory / LEDGER_NAME
    return validate_knowledge_test_db_target(registry), validate_knowledge_test_db_target(ledger)


def _validate_production_paths() -> None:
    current = Path(PRODUCTION_AUTHORITY_ROOT.anchor)
    for part in PRODUCTION_AUTHORITY_ROOT.parts[1:]:
        current /= part
        if current.exists() and is_reparse_point(current): raise ReviewAuthorityMismatch("production review authority rejects linked paths")
    protected = [DEFAULT_REAL_DB_PATH, DEFAULT_SEMANTIC_SIDECAR_PATH, DEFAULT_MEMORY_SIDECAR_PATH, DEFAULT_KNOWLEDGE_SIDECAR_PATH]
    for target in (PRODUCTION_REGISTRY_PATH, PRODUCTION_LEDGER_PATH):
        if is_reparse_point(target): raise ReviewAuthorityMismatch("production review authority rejects linked files")
        for other in protected:
            if other.exists() and os.path.samefile(target, other): raise ReviewAuthorityMismatch("production review authority aliases another production database")


def _open_and_validate(registry_path: Path, ledger_path: Path) -> tuple[ReviewAuthorityIdentity, sqlite3.Connection, sqlite3.Connection]:
    # A mutation commits the append-only ledger immediately before advancing the
    # registry seal.  Cross-process readers may observe that bounded hand-off;
    # retry only this exact transient.  Persistent mismatch still fails closed.
    for attempt in range(10):
        try:
            return _open_and_validate_once(registry_path, ledger_path)
        except ReviewAuthorityStateLost as exc:
            if "rolled back" not in str(exc) or attempt == 9:
                raise
            time.sleep(0.01)
    raise ReviewAuthorityStateLost("review authority validation did not converge")


def _open_and_validate_once(registry_path: Path, ledger_path: Path) -> tuple[ReviewAuthorityIdentity, sqlite3.Connection, sqlite3.Connection]:
    if not registry_path.is_file() or not ledger_path.is_file():
        raise ReviewAuthorityStateLost("review authority registry or ledger is missing")
    registry = sqlite3.connect(f"file:{registry_path.as_posix()}?mode=ro", uri=True)
    ledger = sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True)
    try:
        registry.execute("BEGIN")
        ledger.execute("BEGIN")
        if _actual_schema_fingerprint(registry) != REGISTRY_SCHEMA_FINGERPRINT or _actual_schema_fingerprint(ledger) != LEDGER_SCHEMA_FINGERPRINT:
            raise ReviewAuthorityMismatch("review authority schema fingerprint mismatch")
        row = registry.execute("SELECT authority_store_id,authority_schema_version,authority_generation,ledger_instance_id,initialization_seal,authority_metadata_digest,authority_protocol_identity,registry_schema_fingerprint,ledger_schema_fingerprint FROM authority_registry_metadata WHERE singleton=1").fetchone()
        ledger_row = ledger.execute("SELECT authority_store_id,authority_schema_version,authority_generation,ledger_instance_id,initialization_seal,authority_metadata_digest,authority_protocol_identity,registry_schema_fingerprint,ledger_schema_fingerprint FROM authority_ledger_metadata WHERE singleton=1").fetchone()
        if row is None or ledger_row is None or row != ledger_row:
            raise ReviewAuthorityMismatch("review authority metadata mismatch")
        identity = ReviewAuthorityIdentity(*row)
        if identity.authority_schema_version != AUTHORITY_SCHEMA_VERSION or identity.authority_protocol_identity != AUTHORITY_PROTOCOL_IDENTITY:
            raise ReviewAuthorityMismatch("review authority protocol or schema version is unsupported")
        if identity.authority_metadata_digest != canonical_hash("review_authority_metadata", {key: value for key, value in asdict(identity).items() if key != "authority_metadata_digest"}):
            raise ReviewAuthorityMismatch("review authority metadata seal mismatch")
        registry_state = registry.execute("SELECT mutation_seq,mutation_digest FROM authority_registry_state WHERE singleton=1").fetchone()
        ledger_state = ledger.execute("SELECT mutation_seq,mutation_digest FROM authority_ledger_metadata WHERE singleton=1").fetchone()
        if registry_state is None or ledger_state is None or registry_state != ledger_state:
            raise ReviewAuthorityStateLost("review authority ledger is missing, replaced, or rolled back")
        _validate_authority_contents(registry, ledger, identity, registry_state)
        return identity, registry, ledger
    except Exception:
        registry.close(); ledger.close(); raise


def _validate_pair_connections(registry: sqlite3.Connection, ledger: sqlite3.Connection, identity: ReviewAuthorityIdentity) -> None:
    row = registry.execute("SELECT authority_store_id,authority_schema_version,authority_generation,ledger_instance_id,initialization_seal,authority_metadata_digest,authority_protocol_identity,registry_schema_fingerprint,ledger_schema_fingerprint FROM authority_registry_metadata WHERE singleton=1").fetchone()
    ledger_row = ledger.execute("SELECT authority_store_id,authority_schema_version,authority_generation,ledger_instance_id,initialization_seal,authority_metadata_digest,authority_protocol_identity,registry_schema_fingerprint,ledger_schema_fingerprint FROM authority_ledger_metadata WHERE singleton=1").fetchone()
    if row != tuple(asdict(identity).values()) or ledger_row != row:
        raise ReviewAuthorityMismatch("review authority identity changed")
    if registry.execute("SELECT mutation_seq,mutation_digest FROM authority_registry_state WHERE singleton=1").fetchone() != ledger.execute("SELECT mutation_seq,mutation_digest FROM authority_ledger_metadata WHERE singleton=1").fetchone():
        raise ReviewAuthorityStateLost("review authority mutation heads do not match")
    state = registry.execute("SELECT mutation_seq,mutation_digest FROM authority_registry_state WHERE singleton=1").fetchone()
    _validate_authority_contents(registry, ledger, identity, state)


def _validate_authority_contents(
    registry: sqlite3.Connection,
    ledger: sqlite3.Connection,
    identity: ReviewAuthorityIdentity,
    state: tuple[Any, ...],
) -> None:
    try:
        if registry.execute("PRAGMA quick_check").fetchone() != ("ok",) or ledger.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ReviewAuthorityMismatch("review authority SQLite integrity check failed")
        if ledger.execute("PRAGMA foreign_key_check").fetchall():
            raise ReviewAuthorityMismatch("review authority foreign-key check failed")
        if registry.execute("SELECT COUNT(*) FROM authority_registry_metadata").fetchone() != (1,) or ledger.execute("SELECT COUNT(*) FROM authority_ledger_metadata").fetchone() != (1,):
            raise ReviewAuthorityMismatch("review authority metadata cardinality is invalid")

        candidates: dict[str, RelationCandidate] = {}
        for row in ledger.execute("SELECT candidate_id,candidate_schema_version,candidate_payload_hash,payload_json FROM review_candidates"):
            value = json.loads(row[3]); value["rationale_codes"] = tuple(value["rationale_codes"])
            candidate = RelationCandidate(**value); validate_candidate_identity(candidate)
            if row[:3] != (candidate.candidate_id, candidate.candidate_schema_version, candidate.payload_hash) or row[3] != _json(candidate.public_dict()):
                raise ReviewAuthorityMismatch("stored candidate columns do not match payload")
            candidates[candidate.candidate_id] = candidate

        chains: dict[str, tuple[Any, ...]] = {}
        for row in ledger.execute("SELECT review_chain_id,chain_key,family_identity,candidate_id,candidate_payload_hash,review_policy_identity,predecessor_review_chain_id FROM review_chains"):
            candidate = candidates.get(row[3])
            if candidate is None or row[4] != candidate.payload_hash or row[5] != REVIEW_POLICY_IDENTITY:
                raise ReviewAuthorityMismatch("review chain candidate or policy reference is invalid")
            expected_key = canonical_hash("review_chain_key", {"authority_store_id": identity.authority_store_id, "candidate_id": candidate.candidate_id, "candidate_payload_hash": candidate.payload_hash, "review_policy_identity": REVIEW_POLICY_IDENTITY})
            expected_family = canonical_hash("review_chain_family", {"relation_type": candidate.relation_type, "direction": candidate.direction, "source_node_id": candidate.source_node_id, "target_node_id": candidate.target_node_id, "review_policy_identity": REVIEW_POLICY_IDENTITY})
            expected_chain = "chain_" + canonical_hash("review_chain", {"authority_store_id": identity.authority_store_id, "chain_key": expected_key})
            if row[0] != expected_chain or row[1] != expected_key or row[2] != expected_family:
                raise ReviewAuthorityMismatch("review chain identity is invalid")
            chains[row[0]] = row

        predecessor_rows = ledger.execute("SELECT successor_review_chain_id,predecessor_review_chain_id FROM review_chain_predecessors").fetchall()
        predecessors_by_successor: dict[str, list[str]] = {}
        for successor_id, predecessor_id in predecessor_rows:
            successor, predecessor = chains.get(successor_id), chains.get(predecessor_id)
            if successor is None or predecessor is None or successor_id == predecessor_id or successor[2] != predecessor[2]:
                raise ReviewAuthorityMismatch("review chain predecessor reference is invalid")
            if candidates[successor[3]].graph_build_identity == candidates[predecessor[3]].graph_build_identity:
                raise ReviewAuthorityMismatch("same-build review chains cannot stale each other")
            predecessors_by_successor.setdefault(successor_id, []).append(predecessor_id)

        sessions = {row[0]: row for row in ledger.execute("SELECT review_session_id,review_chain_id,authority_store_id FROM review_sessions")}
        plans: dict[str, ReviewPlan] = {}
        plans_by_chain: dict[str, ReviewPlan] = {}
        for row in ledger.execute("SELECT review_plan_id,review_schema_version,review_chain_id,review_session_id,candidate_id,plan_payload_hash,payload_json FROM review_plans"):
            value = json.loads(row[6]); value["allowed_decisions"] = tuple(value["allowed_decisions"])
            plan = ReviewPlan(**value); validate_review_plan_identity(plan)
            candidate = candidates.get(row[4]); chain = chains.get(row[2]); session = sessions.get(row[3])
            if candidate is None or chain is None or session != (row[3], row[2], identity.authority_store_id):
                raise ReviewAuthorityMismatch("review plan references are invalid")
            _validate_candidate_plan(candidate, plan, identity)
            if row[:6] != (plan.review_plan_id, plan.review_schema_version, plan.review_chain_id, plan.review_session_id, plan.candidate_id, plan.payload_hash) or row[6] != _json(plan.public_dict()):
                raise ReviewAuthorityMismatch("stored review plan columns do not match payload")
            linked = sorted(predecessors_by_successor.get(plan.review_chain_id, []))
            if plan.predecessor_review_chain_id != (linked[0] if linked else None) or chain[6] != plan.predecessor_review_chain_id:
                raise ReviewAuthorityMismatch("review plan predecessor binding is invalid")
            plans[plan.review_plan_id] = plan; plans_by_chain[plan.review_chain_id] = plan

        if not (len(candidates) == len(chains) == len(sessions) == len(plans)):
            raise ReviewAuthorityMismatch("review authority record cardinalities are inconsistent")

        decisions: dict[str, DecisionEnvelope] = {}
        decisions_by_chain: dict[str, DecisionEnvelope] = {}
        for row in ledger.execute("SELECT decision_id,decision_schema_version,review_plan_id,review_chain_id,decision_payload_hash,payload_json FROM review_decisions"):
            envelope = DecisionEnvelope(**json.loads(row[5])); validate_decision_identity(envelope)
            plan = plans.get(row[2])
            if plan is None: raise ReviewAuthorityMismatch("decision references a missing plan")
            _validate_decision_protocol(envelope, plan, identity)
            if row[:5] != (envelope.decision_id, envelope.decision_schema_version, envelope.review_plan_id, envelope.review_chain_id, envelope.payload_hash) or row[5] != _json(envelope.public_dict()):
                raise ReviewAuthorityMismatch("stored decision columns do not match payload")
            decisions[envelope.decision_id] = envelope; decisions_by_chain[envelope.review_chain_id] = envelope

        supersessions: dict[str, SupersessionRecord] = {}
        supersession_by_decision: dict[str, SupersessionRecord] = {}
        for row in ledger.execute("SELECT supersession_id,supersession_schema_version,decision_id,original_review_chain_id,original_candidate_id,replacement_review_chain_id,replacement_candidate_id,payload_json FROM review_supersessions"):
            record = SupersessionRecord(**json.loads(row[7])); validate_supersession_identity(record)
            envelope = decisions.get(row[2]); original = plans_by_chain.get(row[3]); replacement = plans_by_chain.get(row[5])
            if envelope is None or original is None or replacement is None:
                raise ReviewAuthorityMismatch("supersession references are missing")
            _validate_supersession_protocol(record, envelope, original, replacement, identity, candidates[original.candidate_id], candidates[replacement.candidate_id])
            if row[:7] != (record.supersession_id, record.supersession_schema_version, record.decision_id, record.original_review_chain_id, record.original_candidate_id, record.replacement_review_chain_id, record.replacement_candidate_id) or row[7] != _json(record.public_dict()):
                raise ReviewAuthorityMismatch("stored supersession columns do not match payload")
            supersessions[record.supersession_id] = record; supersession_by_decision[record.decision_id] = record

        for envelope in decisions.values():
            if (envelope.decision == "replace") != (envelope.decision_id in supersession_by_decision):
                raise ReviewAuthorityMismatch("replace decision and supersession cardinality mismatch")

        expected_events = {
            ("plan_prepared", _plan_event_identity(plan.review_plan_id, predecessors_by_successor.get(plan.review_chain_id, [])))
            for plan in plans.values()
        } | {
            ("decision_recorded", _decision_event_identity(envelope.decision_id, supersession_by_decision.get(envelope.decision_id).supersession_id if envelope.decision_id in supersession_by_decision else None))
            for envelope in decisions.values()
        }
        event_rows = ledger.execute("SELECT mutation_seq,previous_digest,mutation_digest,event_kind,event_identity FROM authority_events ORDER BY mutation_seq").fetchall()
        if len(event_rows) != int(state[0]) or {(row[3], row[4]) for row in event_rows} != expected_events or len(event_rows) != len(expected_events):
            raise ReviewAuthorityStateLost("review authority events do not cover all records")
        previous = canonical_hash("review_authority_initial_state", asdict(identity))
        for expected_seq, row in enumerate(event_rows, 1):
            seq, stored_previous, digest, event_kind, event_identity = row
            expected_digest = canonical_hash("review_authority_mutation", {"authority_store_id": identity.authority_store_id, "mutation_seq": expected_seq, "previous_digest": previous, "event_kind": event_kind, "event_identity": event_identity})
            if seq != expected_seq or stored_previous != previous or digest != expected_digest:
                raise ReviewAuthorityStateLost("review authority mutation event chain is invalid")
            previous = digest
        if previous != state[1]:
            raise ReviewAuthorityStateLost("review authority mutation head does not match event chain")
    except (ReviewAuthorityMismatch, ReviewAuthorityStateLost):
        raise
    except Exception as exc:
        raise ReviewAuthorityMismatch("review authority stored protocol is invalid") from exc


def _actual_schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name").fetchall()
    return canonical_hash("review_authority_schema", {"objects": [[a, b, " ".join(c.split())] for a, b, c in rows]})


def _persistence_record_from_ledger(
    identity: ReviewAuthorityIdentity, ledger: sqlite3.Connection,
    candidate_id: str, review_plan_id: str, decision_id: str,
) -> dict[str, Any]:
    candidate_row = ledger.execute(
        "SELECT payload_json FROM review_candidates WHERE candidate_id=?", (candidate_id,),
    ).fetchone()
    plan_row = ledger.execute(
        "SELECT payload_json FROM review_plans WHERE review_plan_id=? AND candidate_id=?",
        (review_plan_id, candidate_id),
    ).fetchone()
    decision_row = ledger.execute(
        "SELECT payload_json FROM review_decisions WHERE decision_id=? AND review_plan_id=?",
        (decision_id, review_plan_id),
    ).fetchone()
    if candidate_row is None or plan_row is None or decision_row is None:
        raise ReviewAuthorityMismatch("persistence references are not current authority facts")
    candidate = RelationCandidate(**json.loads(candidate_row[0]))
    plan_value = json.loads(plan_row[0]); plan_value["allowed_decisions"] = tuple(plan_value["allowed_decisions"])
    plan = ReviewPlan(**plan_value)
    decision = DecisionEnvelope(**json.loads(decision_row[0]))
    validate_candidate_identity(candidate); validate_review_plan_identity(plan); validate_decision_identity(decision)
    _validate_candidate_plan(candidate, plan, identity); _validate_decision_protocol(decision, plan, identity)
    if decision.decision != "accept" or decision.status != "accepted_pending_persistence" or decision.persisted:
        raise ReviewAuthorityMismatch("decision is not accepted pending persistence")
    if ledger.execute(
        "SELECT 1 FROM review_supersessions WHERE original_candidate_id=? OR decision_id=?",
        (candidate_id, decision_id),
    ).fetchone():
        raise ReviewAuthorityMismatch("accepted decision has been superseded")
    if ledger.execute(
        "SELECT 1 FROM review_chain_predecessors WHERE predecessor_review_chain_id=?",
        (plan.review_chain_id,),
    ).fetchone():
        raise ReviewAuthorityMismatch("accepted decision belongs to an old review chain")
    mutation_seq, mutation_digest = ledger.execute(
        "SELECT mutation_seq,mutation_digest FROM authority_ledger_metadata WHERE singleton=1"
    ).fetchone()
    return {
        "candidate": candidate, "plan": plan, "decision": decision,
        "authority_identity": identity, "authority_mutation_seq": int(mutation_seq),
        "authority_mutation_digest": str(mutation_digest),
    }


def _load_plan_for_chain(conn: sqlite3.Connection, chain_id: str) -> ReviewPlan:
    row = conn.execute("SELECT payload_json FROM review_plans WHERE review_chain_id=?", (chain_id,)).fetchone()
    if row is None: raise ReviewAuthorityMismatch("review chain has no plan")
    value = json.loads(row[0]); value["allowed_decisions"] = tuple(value["allowed_decisions"])
    return ReviewPlan(**value)


def _chain_status(conn: sqlite3.Connection, chain_id: str) -> str:
    if conn.execute("SELECT 1 FROM review_supersessions WHERE original_review_chain_id=?", (chain_id,)).fetchone(): return "superseded"
    if conn.execute("SELECT 1 FROM review_decisions WHERE review_chain_id=?", (chain_id,)).fetchone(): return "decided"
    if conn.execute("SELECT 1 FROM review_chain_predecessors WHERE predecessor_review_chain_id=?", (chain_id,)).fetchone(): return "stale"
    return "ready"


def _chain_next_action(conn: sqlite3.Connection, chain_id: str) -> str:
    return {"ready": "decide", "decided": "none", "superseded": "review_replacement", "stale": "prepare_successor"}[_chain_status(conn, chain_id)]


def _inspect_chain(conn: sqlite3.Connection, chain_id: str) -> dict[str, Any]:
    status = _chain_status(conn, chain_id)
    if status == "ready": return _state("valid", "review_chain_active", "decide")
    if status == "decided":
        row = conn.execute("SELECT decision_id,payload_json FROM review_decisions WHERE review_chain_id=?", (chain_id,)).fetchone(); payload = json.loads(row[1])
        return {**_state("decided", "decision_already_recorded", "none"), "decision_id": row[0], "decision": payload["decision"]}
    if status == "superseded":
        row = conn.execute("SELECT supersession_id,replacement_candidate_id,replacement_review_chain_id FROM review_supersessions WHERE original_review_chain_id=?", (chain_id,)).fetchone()
        return {**_state("superseded", "review_chain_superseded", "review_replacement"), "supersession_id": row[0], "replacement_candidate_id": row[1], "replacement_review_chain_id": row[2]}
    return _state("stale", "successor_review_chain_exists", "prepare_successor")


def _validate_candidate_plan(candidate: RelationCandidate, plan: ReviewPlan, identity: ReviewAuthorityIdentity) -> None:
    validate_candidate_identity(candidate); validate_review_plan_identity(plan)
    if plan.candidate_id != candidate.candidate_id or plan.candidate_payload_hash != candidate.payload_hash: raise RelationReviewError("review plan candidate reference mismatch")
    candidate_fields = (
        candidate.source_node_id, candidate.source_node_payload_hash,
        candidate.target_node_id, candidate.target_node_payload_hash,
        candidate.relation_type, candidate.direction, candidate.evidence_digest,
        candidate.generator_identity, candidate.generator_config_hash,
        candidate.graph_build_identity, candidate.score, candidate.threshold,
    )
    plan_fields = (
        plan.source_node_id, plan.source_node_payload_hash,
        plan.target_node_id, plan.target_node_payload_hash,
        plan.relation_type, plan.direction, plan.evidence_digest,
        plan.generator_identity, plan.generator_config_hash,
        plan.graph_build_identity, plan.score, plan.threshold,
    )
    if plan_fields != candidate_fields: raise RelationReviewError("review plan does not fully bind its candidate")
    if (plan.authority_store_id, plan.authority_schema_version, plan.authority_generation, plan.authority_metadata_digest, plan.authority_protocol_identity) != (identity.authority_store_id, identity.authority_schema_version, identity.authority_generation, identity.authority_metadata_digest, identity.authority_protocol_identity): raise ReviewAuthorityMismatch("review plan authority reference mismatch")
    if plan.review_policy_identity != REVIEW_POLICY_IDENTITY: raise RelationReviewError("review plan policy is unsupported")


def _validate_decision_protocol(envelope: DecisionEnvelope, plan: ReviewPlan, identity: ReviewAuthorityIdentity) -> None:
    if (envelope.review_plan_id, envelope.candidate_id, envelope.candidate_payload_hash,
        envelope.review_chain_id, envelope.review_session_id) != (
        plan.review_plan_id, plan.candidate_id, plan.candidate_payload_hash,
        plan.review_chain_id, plan.review_session_id): raise RelationReviewError("decision references do not match active plan")
    authority_fields = (envelope.authority_store_id, envelope.authority_schema_version,
        envelope.authority_generation, envelope.authority_metadata_digest,
        envelope.authority_protocol_identity)
    if authority_fields != (identity.authority_store_id, identity.authority_schema_version,
        identity.authority_generation, identity.authority_metadata_digest,
        identity.authority_protocol_identity): raise RelationReviewError("decision authority mismatch")
    if (envelope.relation_type, envelope.source_node_id, envelope.target_node_id,
        envelope.generator_identity, envelope.evidence_digest) != (
        plan.relation_type, plan.source_node_id, plan.target_node_id,
        plan.generator_identity, plan.evidence_digest): raise RelationReviewError("decision candidate binding mismatch")
    if envelope.decision not in plan.allowed_decisions: raise RelationReviewError("decision allowlist mismatch")
    if envelope.decision != "replace" and envelope.confirmation_fields != required_confirmation_fields(plan, envelope.decision):
        raise RelationReviewError("decision confirmation does not exactly match plan")


def _validate_supersession_protocol(
    record: SupersessionRecord,
    envelope: DecisionEnvelope,
    original: ReviewPlan,
    replacement: ReviewPlan,
    identity: ReviewAuthorityIdentity,
    original_candidate: RelationCandidate,
    replacement_candidate: RelationCandidate,
) -> None:
    if envelope.decision != "replace" or record.decision_id != envelope.decision_id: raise RelationReviewError("supersession requires its replace decision")
    if record.authority_store_id != identity.authority_store_id or record.original_review_chain_id != original.review_chain_id or record.replacement_review_chain_id != replacement.review_chain_id: raise RelationReviewError("supersession authority or chain reference mismatch")
    if (record.review_session_id, record.original_candidate_id, record.original_candidate_payload_hash,
        record.original_review_plan_id, record.original_plan_payload_hash,
        record.replacement_candidate_id, record.replacement_candidate_payload_hash,
        record.replacement_review_plan_id, record.replacement_plan_payload_hash) != (
        original.review_session_id, original.candidate_id, original.candidate_payload_hash,
        original.review_plan_id, original.payload_hash,
        replacement.candidate_id, replacement.candidate_payload_hash,
        replacement.review_plan_id, replacement.payload_hash):
        raise RelationReviewError("supersession references do not fully match both plans")
    if original.review_chain_id == replacement.review_chain_id: raise RelationReviewError("supersession cannot reference itself")
    original_family = (original.relation_type, original.direction, original.source_node_id, original.target_node_id)
    replacement_family = (replacement.relation_type, replacement.direction, replacement.source_node_id, replacement.target_node_id)
    if original_family != replacement_family or original.review_policy_identity != replacement.review_policy_identity:
        raise RelationReviewError("supersession replacement family or policy is invalid")
    if (envelope.replacement_candidate_id, envelope.replacement_review_plan_id,
        envelope.replacement_candidate_payload_hash, envelope.replacement_plan_payload_hash) != (
        replacement.candidate_id, replacement.review_plan_id,
        replacement.candidate_payload_hash, replacement.payload_hash):
        raise RelationReviewError("replace decision does not bind the referenced replacement plan")
    if envelope.confirmation_fields != required_confirmation_fields(
        original, "replace", replacement_candidate=replacement_candidate,
        replacement_plan=replacement,
    ):
        raise RelationReviewError("replace confirmation does not exactly bind both candidates and plans")


def _envelope(payload: str) -> DecisionEnvelope:
    return DecisionEnvelope(**json.loads(payload))


def _state(status: str, reason_code: str, next_action: str) -> dict[str, Any]:
    return {"status": status, "reason_code": reason_code, "next_action": next_action, "persisted": False, "readonly": True, "coordination_recorded": status not in {"stale", "authority_state_lost"}, "review_state": "recorded" if status in {"valid", "decided", "superseded"} else "conflict" if status in {"decision_conflict", "manual_review"} else "unavailable"}


def _plan_event_identity(review_plan_id: str, predecessor_chain_ids: list[str] | tuple[str, ...]) -> str:
    return "event_" + canonical_hash("review_authority_plan_event", {
        "review_plan_id": review_plan_id,
        "stale_predecessor_chain_ids": sorted(predecessor_chain_ids),
    })


def _decision_event_identity(decision_id: str, supersession_id: str | None) -> str:
    return "event_" + canonical_hash("review_authority_decision_event", {
        "decision_id": decision_id,
        "supersession_id": supersession_id,
    })


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
