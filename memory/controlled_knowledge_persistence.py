"""YunOS v6.2 controlled persistence for authority-accepted relations."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from memory.current_relation_discovery import (
    CurrentRelationDiscoveryProvider,
    is_trusted_current_discovery_provider,
)
from memory.db_path_policy import validate_knowledge_test_db_target
from memory.graph_identity import canonical_hash
from memory.knowledge_sidecar import (
    KNOWLEDGE_SCHEMA_FINGERPRINT,
    KNOWLEDGE_SCHEMA_VERSION,
    REQUIRED_TABLES,
    _schema_fingerprint,
    load_knowledge_store_identity,
)
from memory.relation_review import CandidateDiscovery, verify_review_plan


PERSISTENCE_PROTOCOL_IDENTITY = "yunos_controlled_knowledge_persistence_v2"
RELATION_VERSION = 1


class ControlledPersistenceError(RuntimeError):
    pass


class SimulatedInterruption(ControlledPersistenceError):
    pass


@dataclass(frozen=True)
class PersistenceRequest:
    candidate_id: str
    review_plan_id: str
    decision_id: str


@dataclass(frozen=True)
class AnchorAssessment:
    enabled: bool
    satisfied: bool
    anchor_identity: str | None
    assertion_digest: str | None
    reason: str


class ExternalStateAnchor(Protocol):
    def assess(self, *, authority_store_id: str, mutation_seq: int, mutation_digest: str) -> AnchorAssessment:
        ...


class DisabledExternalStateAnchor:
    def assess(self, *, authority_store_id: str, mutation_seq: int, mutation_digest: str) -> AnchorAssessment:
        return AnchorAssessment(False, False, None, None, "external_anchor_not_configured")


def production_enablement_conditions() -> dict[str, Any]:
    return {
        "production_enabled": False,
        "required": [
            "independently_approved_production_initializer",
            "explicit_operator_write_authorization",
            "validated_backup_and_restore_location",
            "durable_external_authority_anchor",
            "external_anchor_matches_current_authority_mutation_head",
            "independent_security_and_recovery_review",
        ],
        "network_used": False,
        "external_service_called": False,
    }


def persist_accepted_relation(
    authority: Any,
    request: PersistenceRequest,
    *,
    discovery_provider: CurrentRelationDiscoveryProvider,
    sidecar_path: str | Path,
    backup_dir: str | Path,
    execute: bool = False,
    test_mode: bool = False,
    confirm_persistence_id: str | None = None,
    external_anchor: ExternalStateAnchor | None = None,
    _failpoint: str | None = None,
) -> dict[str, Any]:
    if not test_mode:
        raise ControlledPersistenceError("production knowledge persistence is not enabled in v6.2")
    if not is_trusted_current_discovery_provider(discovery_provider):
        raise ControlledPersistenceError("trusted current discovery provider is required")
    target = validate_knowledge_test_db_target(sidecar_path)
    backups = _validate_temp_directory(backup_dir)
    if not target.is_file():
        raise ControlledPersistenceError("knowledge sidecar must be independently initialized")
    if not execute:
        facts = authority.current_persistence_record(
            request.candidate_id, request.review_plan_id, request.decision_id,
        )
        discovery = discovery_provider.rebuild()
        _revalidate_current_facts(discovery, authority, request, facts)
        conn = _open_store(target)
        try:
            store = _require_v3_schema(conn)
            _verify_current_nodes(conn, discovery, facts["candidate"])
        finally:
            conn.close()
        anchor = _assess_anchor(external_anchor, facts)
        return _preview_payload(request, facts, store, anchor)

    lock_path = target.with_suffix(target.suffix + ".coordination.lock")
    lock_fd: int | None = None
    conn: sqlite3.Connection | None = None
    try:
        lock_fd = _acquire_lock(lock_path)
        with authority.reserve_persistence(
            request.candidate_id, request.review_plan_id, request.decision_id,
        ) as facts:
            discovery = discovery_provider.rebuild()
            _revalidate_current_facts(discovery, authority, request, facts)
            conn = _open_store(target)
            store = _require_v3_schema(conn)
            _verify_current_nodes(conn, discovery, facts["candidate"])
            anchor = _assess_anchor(external_anchor, facts)
            current = _preview_payload(request, facts, store, anchor)
            existing = _existing_receipt(conn, facts["decision"].decision_id)
            if existing:
                if existing[0] != confirm_persistence_id:
                    raise ControlledPersistenceError("persisted decision does not match the confirmed persistence identity")
                return {**current, "status": "already_persisted", "executed": True,
                        "persisted": True, "idempotent": True, "receipt_id": existing[1]}
            if confirm_persistence_id != current["persistence_id"]:
                raise ControlledPersistenceError("exact current persistence ID confirmation is required")
            _reject_active_endpoint(conn, facts["candidate"])
            conn.close(); conn = None

            backup = _create_backup(target, backups, current, store, facts)
            if _failpoint == "after_backup":
                raise SimulatedInterruption("simulated interruption after durable backup")

            # Authority reservation remains held. Rebuild current sources again,
            # then verify the exact store identity inside the write transaction.
            discovery = discovery_provider.rebuild()
            _revalidate_current_facts(discovery, authority, request, facts)
            conn = sqlite3.connect(target, timeout=30)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            locked_store = _require_v3_schema(conn)
            if _store_binding(locked_store) != _store_binding(store):
                raise ControlledPersistenceError("knowledge store identity changed after confirmation")
            _verify_current_nodes(conn, discovery, facts["candidate"])
            existing = _existing_receipt(conn, facts["decision"].decision_id)
            if existing:
                if existing[0] != confirm_persistence_id:
                    raise ControlledPersistenceError("persisted decision identity collision")
                conn.rollback()
                return {**current, "status": "already_persisted", "executed": True,
                        "persisted": True, "idempotent": True, "receipt_id": existing[1]}
            _reject_active_endpoint(conn, facts["candidate"])
            result = _insert_transaction(conn, current, store, facts, anchor, backup)
            if _failpoint in {"before_commit", "hard_before_commit"}:
                if _failpoint.startswith("hard_"): os._exit(91)
                raise SimulatedInterruption("simulated interruption before commit")
            conn.commit()
            if _failpoint in {"after_commit", "hard_after_commit"}:
                if _failpoint.startswith("hard_"): os._exit(92)
                raise SimulatedInterruption("simulated interruption after commit")
            return {**current, **result, "status": "persisted", "executed": True,
                    "persisted": True, "idempotent": False,
                    "backup_manifest": str(backup["manifest_path"])}
    except Exception:
        if conn is not None and conn.in_transaction: conn.rollback()
        raise
    finally:
        if conn is not None: conn.close()
        if lock_fd is not None: _release_lock(lock_fd)


def restore_test_sidecar(
    sidecar_path: str | Path, manifest_path: str | Path, *,
    execute: bool = False, confirm_restore_id: str | None = None,
    _failpoint: str | None = None,
) -> dict[str, Any]:
    target = validate_knowledge_test_db_target(sidecar_path)
    manifest_file = Path(manifest_path).absolute()
    _validate_temp_directory(manifest_file.parent)
    lock_fd = _acquire_lock(target.with_suffix(target.suffix + ".coordination.lock"))
    try:
        authorization = _restore_authorization(target, manifest_file)
        if not execute:
            return {**authorization, "status": "restore_validated", "executed": False, "restored": False}
        if confirm_restore_id != authorization["restore_id"]:
            raise ControlledPersistenceError("exact restore authorization is required")
        backup_path = validate_knowledge_test_db_target(authorization["backup_path"])
        source = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        destination = sqlite3.connect(target, timeout=30)
        try:
            if _failpoint == "hard_during_restore":
                source.backup(destination, pages=1, progress=lambda *_: os._exit(93))
            else:
                source.backup(destination)
            destination.execute("PRAGMA foreign_keys=ON")
            restored_store = _require_v3_schema(destination)
            if _store_binding(restored_store) != authorization["store_binding"]:
                raise ControlledPersistenceError("restored knowledge store identity mismatch")
        finally:
            destination.close(); source.close()
        receipt_path = manifest_file.with_name(f"restore-{authorization['restore_id']}.receipt.json")
        _write_exclusive_json(receipt_path, {
            "restore_id": authorization["restore_id"], "manifest_digest": authorization["manifest_digest"],
            "knowledge_store_id": restored_store["knowledge_store_id"], "restored_at": _utc_now(),
            "production_eligible": False,
        })
        return {**authorization, "status": "restored", "executed": True, "restored": True,
                "recovery_receipt": str(receipt_path)}
    finally:
        _release_lock(lock_fd)


def _preview_payload(request: PersistenceRequest, facts: dict[str, Any], store: dict[str, Any], anchor: AnchorAssessment) -> dict[str, Any]:
    candidate, plan, decision = facts["candidate"], facts["plan"], facts["decision"]
    payload = {
        "protocol": PERSISTENCE_PROTOCOL_IDENTITY,
        "candidate_id": candidate.candidate_id, "candidate_payload_hash": candidate.payload_hash,
        "review_plan_id": plan.review_plan_id, "plan_payload_hash": plan.payload_hash,
        "decision_id": decision.decision_id, "decision_payload_hash": decision.payload_hash,
        "authority_store_id": plan.authority_store_id, "authority_generation": plan.authority_generation,
        "authority_mutation_seq": facts["authority_mutation_seq"],
        "authority_mutation_digest": facts["authority_mutation_digest"],
        **_store_binding(store), **_anchor_binding(anchor),
    }
    persistence_id = "persistence_" + canonical_hash("accepted_relation_persistence", payload)
    return {
        "status": "validated", "executable": True, "executed": False, "persisted": False,
        "persistence_id": persistence_id, "candidate_id": request.candidate_id,
        "review_plan_id": request.review_plan_id, "decision_id": request.decision_id,
        **_store_binding(store), "authority_mutation_seq": facts["authority_mutation_seq"],
        "authority_mutation_digest": facts["authority_mutation_digest"],
        "external_anchor_enabled": anchor.enabled, "external_anchor_satisfied": anchor.satisfied,
        "external_anchor_identity": anchor.anchor_identity,
        "external_anchor_assertion_digest": anchor.assertion_digest,
        "production_eligible": False, "test_mode": True,
    }


def _revalidate_current_facts(discovery: CandidateDiscovery, authority: Any, request: PersistenceRequest, facts: dict[str, Any]) -> None:
    discovery.assert_trusted()
    current = discovery.candidate(request.candidate_id)
    if current.candidate_id != facts["candidate"].candidate_id or current.immutable_payload() != facts["candidate"].immutable_payload():
        raise ControlledPersistenceError("current rebuilt candidate does not match review authority")
    verification = verify_review_plan(discovery, facts["plan"], authority=authority)
    if verification.get("status") != "decided" or verification.get("decision_id") != request.decision_id or verification.get("decision") != "accept":
        raise ControlledPersistenceError("current plan or decision is not persistable")
    nodes = {node.node_id for node in discovery.nodes}
    if current.source_node_id not in nodes or current.target_node_id not in nodes:
        raise ControlledPersistenceError("current graph nodes are unavailable")


def _assess_anchor(anchor: ExternalStateAnchor | None, facts: dict[str, Any]) -> AnchorAssessment:
    assessment = (anchor or DisabledExternalStateAnchor()).assess(
        authority_store_id=facts["plan"].authority_store_id,
        mutation_seq=facts["authority_mutation_seq"], mutation_digest=facts["authority_mutation_digest"],
    )
    if not isinstance(assessment.reason, str) or not assessment.reason:
        raise ControlledPersistenceError("external anchor assessment reason is required")
    if assessment.enabled:
        if not assessment.satisfied or not assessment.anchor_identity or not assessment.assertion_digest:
            raise ControlledPersistenceError("configured external anchor is not satisfied")
    elif assessment.satisfied or assessment.anchor_identity is not None or assessment.assertion_digest is not None:
        raise ControlledPersistenceError("disabled external anchor assessment is inconsistent")
    return assessment


def _anchor_binding(anchor: AnchorAssessment) -> dict[str, Any]:
    return {
        "anchor_enabled": anchor.enabled, "anchor_satisfied": anchor.satisfied,
        "anchor_identity": anchor.anchor_identity, "anchor_assertion_digest": anchor.assertion_digest,
        "anchor_reason_digest": canonical_hash("external_anchor_reason", {"reason": anchor.reason}),
    }


def _store_binding(store: dict[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_store_id": store["knowledge_store_id"],
        "knowledge_generation": store["knowledge_generation"],
        "knowledge_initialization_seal": store["initialization_seal"],
        "knowledge_schema_fingerprint": store["schema_fingerprint"],
        "knowledge_metadata_digest": store["metadata_digest"],
    }


def _open_store(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _require_v3_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ControlledPersistenceError("knowledge sidecar integrity check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ControlledPersistenceError("knowledge sidecar foreign key check failed")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES.issubset(tables) or conn.execute("PRAGMA user_version").fetchone()[0] != KNOWLEDGE_SCHEMA_VERSION:
        raise ControlledPersistenceError("knowledge sidecar v6.2 schema is unavailable")
    if _schema_fingerprint(conn) != KNOWLEDGE_SCHEMA_FINGERPRINT:
        raise ControlledPersistenceError("knowledge sidecar schema fingerprint mismatch")
    try: return load_knowledge_store_identity(conn)
    except Exception as exc: raise ControlledPersistenceError("knowledge store identity validation failed") from exc


def _verify_current_nodes(conn: sqlite3.Connection, discovery: CandidateDiscovery, candidate: Any) -> None:
    nodes = {node.node_id: node for node in discovery.nodes}
    for node_id, expected_hash in ((candidate.source_node_id, candidate.source_node_payload_hash),
                                   (candidate.target_node_id, candidate.target_node_payload_hash)):
        node = nodes[node_id]
        if canonical_hash("graph_node_payload", node.public_dict()) != expected_hash:
            raise ControlledPersistenceError("current graph node payload hash mismatch")
        row = conn.execute(
            "SELECT node_schema_version,node_type,source_table,source_id,conversation_id,summary_version,summary_text_hash,source_snapshot_hash,memory_space FROM graph_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        expected = (node.node_schema_version,node.node_type,node.source_table,node.source_id,node.conversation_id,
                    node.summary_version,node.summary_text_hash,node.source_snapshot_hash,node.memory_space)
        if row != expected: raise ControlledPersistenceError("knowledge sidecar graph node identity mismatch")


def _existing_receipt(conn: sqlite3.Connection, decision_id: str) -> tuple[str, str] | None:
    return conn.execute(
        "SELECT persistence_id,receipt_id FROM accepted_relation_versions JOIN persistence_receipts USING(persistence_id) WHERE decision_id=?",
        (decision_id,),
    ).fetchone()


def _reject_active_endpoint(conn: sqlite3.Connection, candidate: Any) -> None:
    if conn.execute(
        "SELECT 1 FROM accepted_relation_versions WHERE relation_type=? AND source_node_id=? AND target_node_id=? AND status='active'",
        (candidate.relation_type,candidate.source_node_id,candidate.target_node_id),
    ).fetchone(): raise ControlledPersistenceError("formal relation endpoints are already active under another decision")


def _insert_transaction(conn: sqlite3.Connection, current: dict[str, Any], store: dict[str, Any], facts: dict[str, Any], anchor: AnchorAssessment, backup: dict[str, Any]) -> dict[str, Any]:
    candidate, plan, decision = facts["candidate"], facts["plan"], facts["decision"]
    persisted_at = _utc_now()
    transaction_identity = "transaction_" + canonical_hash("knowledge_persistence_transaction", {
        "persistence_id": current["persistence_id"], "backup_identity": backup["backup_identity"],
        "authority_mutation_seq": facts["authority_mutation_seq"], "authority_mutation_digest": facts["authority_mutation_digest"],
        **_store_binding(store), **_anchor_binding(anchor),
    })
    receipt_id = "receipt_" + canonical_hash("knowledge_persistence_receipt", {"transaction_identity": transaction_identity})
    conn.execute("INSERT INTO accepted_relation_versions VALUES(" + ",".join("?" for _ in range(31)) + ")", (
        current["persistence_id"],KNOWLEDGE_SCHEMA_VERSION,RELATION_VERSION,store["knowledge_store_id"],store["knowledge_generation"],store["metadata_digest"],
        candidate.candidate_id,candidate.payload_hash,plan.review_plan_id,plan.payload_hash,decision.decision_id,decision.payload_hash,
        plan.authority_store_id,plan.authority_generation,plan.authority_metadata_digest,plan.review_chain_id,candidate.relation_type,
        candidate.source_node_id,candidate.source_node_payload_hash,candidate.target_node_id,candidate.target_node_payload_hash,candidate.direction,
        candidate.generator_identity,candidate.generator_config_hash,candidate.evidence_digest,candidate.graph_build_identity,candidate.score,
        candidate.threshold,decision.decided_at,persisted_at,"active"))
    binding = _anchor_binding(anchor)
    conn.execute("INSERT INTO persistence_receipts VALUES(" + ",".join("?" for _ in range(17)) + ")", (
        receipt_id,current["persistence_id"],store["knowledge_store_id"],store["knowledge_generation"],store["metadata_digest"],
        transaction_identity,backup["backup_identity"],facts["authority_mutation_seq"],facts["authority_mutation_digest"],1,
        int(anchor.enabled),int(anchor.satisfied),anchor.anchor_identity,anchor.assertion_digest,binding["anchor_reason_digest"],0,persisted_at))
    return {"receipt_id":receipt_id,"transaction_identity":transaction_identity,"backup_identity":backup["backup_identity"]}


def _create_backup(target: Path, backup_dir: Path, current: dict[str, Any], store: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    stamp = _utc_now().replace(":","").replace("-","").replace(".","")
    backup_path = backup_dir / f"knowledge-before-{current['persistence_id'][:24]}-{stamp}.db"
    validate_knowledge_test_db_target(backup_path)
    source=sqlite3.connect(f"file:{target.as_posix()}?mode=ro",uri=True); destination=sqlite3.connect(backup_path)
    try: source.backup(destination)
    finally: destination.close(); source.close()
    backup_sha256=_sha256_file(backup_path)
    with backup_path.open("r+b") as stream: os.fsync(stream.fileno())
    backup_identity="backup_"+canonical_hash("knowledge_sidecar_backup",{
        "persistence_id":current["persistence_id"],"backup_sha256":backup_sha256,**_store_binding(store),
        "authority_mutation_seq":facts["authority_mutation_seq"],"authority_mutation_digest":facts["authority_mutation_digest"]})
    manifest={"manifest_schema_version":"v6.2-2","backup_identity":backup_identity,"persistence_id":current["persistence_id"],
              "sidecar_path":str(target),"backup_path":str(backup_path),"backup_sha256":backup_sha256,
              **_store_binding(store),"authority_mutation_seq":facts["authority_mutation_seq"],
              "authority_mutation_digest":facts["authority_mutation_digest"],"created_at":_utc_now(),"production_eligible":False}
    manifest["manifest_digest"]=canonical_hash("knowledge_backup_manifest",manifest)
    manifest_path=backup_path.with_suffix(".manifest.json"); _write_exclusive_json(manifest_path,manifest)
    return {**manifest,"manifest_path":manifest_path}


def _restore_authorization(target: Path, manifest_file: Path) -> dict[str, Any]:
    manifest=json.loads(manifest_file.read_text(encoding="utf-8")); supplied=manifest.pop("manifest_digest",None)
    expected=canonical_hash("knowledge_backup_manifest",manifest)
    if supplied!=expected: raise ControlledPersistenceError("backup manifest digest mismatch")
    backup_path=validate_knowledge_test_db_target(manifest["backup_path"])
    if Path(manifest.get("sidecar_path","")).absolute()!=target: raise ControlledPersistenceError("backup manifest belongs to another sidecar")
    if not backup_path.is_file() or _sha256_file(backup_path)!=manifest["backup_sha256"]: raise ControlledPersistenceError("backup file checksum mismatch")
    current_conn=_open_store(target); backup_conn=_open_store(backup_path)
    try: current_store=_require_v3_schema(current_conn); backup_store=_require_v3_schema(backup_conn)
    finally: current_conn.close(); backup_conn.close()
    binding=_store_binding(current_store)
    if binding!=_store_binding(backup_store) or any(manifest.get(k)!=v for k,v in binding.items()):
        raise ControlledPersistenceError("backup knowledge store identity mismatch")
    current_sha=_sha256_file(target)
    restore_id="restore_"+canonical_hash("knowledge_restore_authorization",{
        "manifest_digest":supplied,"current_sha256":current_sha,**binding})
    return {"restore_id":restore_id,"manifest_digest":supplied,"backup_identity":manifest["backup_identity"],
            "backup_path":str(backup_path),"store_binding":binding}


def _validate_temp_directory(value: str | Path) -> Path:
    directory=Path(value).expanduser().absolute(); validate_knowledge_test_db_target(directory/"path-policy-probe.db")
    if not directory.is_dir(): raise ControlledPersistenceError("backup directory must already exist")
    return directory


def _acquire_lock(path: Path) -> int:
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        if os.fstat(descriptor).st_size==0: os.write(descriptor,b"0"); os.fsync(descriptor)
        os.lseek(descriptor,0,os.SEEK_SET)
        if os.name=="nt":
            import msvcrt; msvcrt.locking(descriptor,msvcrt.LK_NBLCK,1)
        else:
            import fcntl; fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return descriptor
    except (OSError,ImportError) as exc:
        os.close(descriptor); raise ControlledPersistenceError("another persistence or restore operation is active") from exc


def _release_lock(descriptor: int) -> None:
    try:
        os.lseek(descriptor,0,os.SEEK_SET)
        if os.name=="nt":
            import msvcrt; msvcrt.locking(descriptor,msvcrt.LK_UNLCK,1)
        else:
            import fcntl; fcntl.flock(descriptor,fcntl.LOCK_UN)
    finally: os.close(descriptor)


def _write_exclusive_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
        json.dump(payload,stream,ensure_ascii=True,sort_keys=True,separators=(",",":")); stream.flush(); os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
