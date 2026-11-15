"""Read-only supplemental verification for the immutable v10-C real call.

This tool must never dispatch a Provider request.  It records the fixed
loopback Search proof, value-level credential non-persistence, conservative
unknown-cost accounting, and the exact user-consent evidence that preceded the
already completed one-shot call.  It does not rewrite the original sentry or
acceptance artifact and explicitly records their historical limitations.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable
from urllib.parse import quote, quote_plus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.deepseek_v4_adapter import load_production_deepseek_api_key
from memory.operational_state_models import canonical_digest
from memory.production_runtime_execution import ProductionRuntimeExecutionService
from tools.v10_c_real_acceptance import (
    EXTERNAL_SEND_DISCLOSURE,
    EXTERNAL_SEND_DISCLOSURE_DIGEST,
    SEARCH_LOCAL_ENDPOINT,
    USER_CONSENT_UTTERANCE,
    _database_snapshot,
    _runtime_status,
    _search_zero_model_proof,
    _utc_now,
)


SUPPLEMENT_VERSION = "v10-c-post-call-verification-1"
OUTPUT_DIR = PROJECT_ROOT / "output" / "v10-c"
ORIGINAL_REPORT = OUTPUT_DIR / "real-deepseek-acceptance.json"
ORIGINAL_SENTRY = OUTPUT_DIR / "real-deepseek-one-shot-sentry.json"
SUPPLEMENT_PATH = OUTPUT_DIR / "post-call-verification.json"
RUNTIME_DB = PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db"
ORIGINAL_COST_RESERVATION_CEILING_MICROUNITS = 20_000_000


class PostCallVerificationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PostCallVerificationError(
            f"invalid_json_artifact:{path.name}"
        ) from exc
    if not isinstance(value, dict):
        raise PostCallVerificationError(f"invalid_json_artifact:{path.name}")
    return value


def _git_paths(arguments: list[str]) -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", *arguments, "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    values: list[Path] = []
    for encoded in completed.stdout.split(b"\x00"):
        if not encoded:
            continue
        relative = encoded.decode("utf-8", errors="strict")
        parts = Path(relative).parts
        if parts and parts[0].casefold() == "config_private":
            continue
        values.append(PROJECT_ROOT / relative)
    return tuple(values)


def _secret_variants(secret: str) -> tuple[bytes, ...]:
    text_variants = {
        secret,
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
        base64.b64encode(secret.encode("utf-8")).decode("ascii"),
    }
    encoded = {
        value.encode(encoding)
        for value in text_variants
        for encoding in ("utf-8", "utf-16-le", "utf-16-be")
    }
    return tuple(sorted(encoded, key=lambda value: (len(value), value)))


def _stream_contains(path: Path, patterns: tuple[bytes, ...]) -> bool:
    overlap_size = max(len(pattern) for pattern in patterns) - 1
    overlap = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return False
            window = overlap + block
            if any(pattern in window for pattern in patterns):
                return True
            overlap = window[-overlap_size:] if overlap_size else b""


def _safe_candidates(paths: Iterable[Path]) -> tuple[tuple[Path, ...], list[str]]:
    root = PROJECT_ROOT.resolve()
    candidates: dict[str, Path] = {}
    unsafe: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink():
            unsafe.append(str(path))
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            unsafe.append(str(path))
            continue
        if resolved.is_file():
            candidates[str(resolved).casefold()] = resolved
    return tuple(candidates[key] for key in sorted(candidates)), unsafe


def _credential_persistence_scan() -> dict[str, Any]:
    secret = load_production_deepseek_api_key()
    try:
        patterns = _secret_variants(secret)
        tracked = _git_paths(["ls-files"])
        untracked = _git_paths(["ls-files", "--others", "--exclude-standard"])
        explicit = [ORIGINAL_REPORT, ORIGINAL_SENTRY, RUNTIME_DB]
        for suffix in ("-wal", "-shm"):
            explicit.append(Path(f"{RUNTIME_DB}{suffix}"))
        candidates, unsafe = _safe_candidates((*tracked, *untracked, *explicit))
        matches: list[str] = []
        aggregate: list[dict[str, Any]] = []
        explicit_set = {
            str(path.resolve()).casefold()
            for path in explicit
            if path.exists() and not path.is_symlink()
        }
        explicit_records: list[dict[str, Any]] = []
        for path in candidates:
            matched = _stream_contains(path, patterns)
            if matched:
                matches.append(str(path.relative_to(PROJECT_ROOT)))
            record = {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            aggregate.append(record)
            if str(path).casefold() in explicit_set:
                explicit_records.append(record)
        return {
            "scan_kind": "exact_credential_value_and_common_encodings",
            "credential_loaded_from_fixed_private_resolver": True,
            "credential_value_or_digest_recorded": False,
            "excluded_expected_private_root": "config_private",
            "candidate_file_count": len(candidates),
            "candidate_manifest_digest": canonical_digest(aggregate),
            "explicit_artifacts": explicit_records,
            "unsafe_candidate_paths": unsafe,
            "exact_credential_match_paths": matches,
            "exact_credential_match_count": len(matches),
            "passed": not unsafe and not matches,
        }
    finally:
        secret = ""


def _original_artifact_evidence(
    report: dict[str, Any],
    sentry: dict[str, Any],
) -> dict[str, Any]:
    report_payload = {
        key: value for key, value in report.items() if key != "report_digest"
    }
    report_digest_valid = report.get("report_digest") == canonical_digest(
        report_payload
    )
    response_text = report.get("runtime", {}).get("response_text", "")
    response_hash_valid = (
        isinstance(response_text, str)
        and report.get("runtime", {}).get("response_sha256")
        == hashlib.sha256(response_text.encode("utf-8")).hexdigest()
    )
    return {
        "report": {
            "path": str(ORIGINAL_REPORT.relative_to(PROJECT_ROOT)),
            "size": ORIGINAL_REPORT.stat().st_size,
            "sha256": _sha256(ORIGINAL_REPORT),
            "canonical_digest": report.get("report_digest"),
            "canonical_digest_valid": report_digest_valid,
            "status": report.get("status"),
        },
        "sentry": {
            "path": str(ORIGINAL_SENTRY.relative_to(PROJECT_ROOT)),
            "size": ORIGINAL_SENTRY.stat().st_size,
            "sha256": _sha256(ORIGINAL_SENTRY),
            "canonical_digest": canonical_digest(sentry),
            "state": sentry.get("state"),
            "do_not_rerun": sentry.get("do_not_rerun"),
        },
        "cross_binding": {
            "model_run_id": report.get("runtime", {}).get("model_run_id"),
            "model_run_matches_sentry": (
                report.get("runtime", {}).get("model_run_id")
                == sentry.get("runtime_model_run_id")
            ),
            "objective_digest_matches": (
                report.get("objective", {}).get("canonical_digest")
                == sentry.get("objective_digest")
            ),
            "context_digest_matches": (
                report.get("context", {}).get("context_digest")
                == sentry.get("context_digest")
            ),
            "response_hash_valid": response_hash_valid,
        },
    }


def run() -> dict[str, Any]:
    if SUPPLEMENT_PATH.exists():
        raise PostCallVerificationError("supplement_exists_do_not_rewrite")
    if not ORIGINAL_REPORT.is_file() or not ORIGINAL_SENTRY.is_file():
        raise PostCallVerificationError("original_one_shot_artifacts_missing")
    report = _load_json(ORIGINAL_REPORT)
    sentry = _load_json(ORIGINAL_SENTRY)
    runtime_before = _runtime_status()
    database_before = _database_snapshot()
    original = _original_artifact_evidence(report, sentry)
    model_run_id = report.get("runtime", {}).get("model_run_id")
    readback = ProductionRuntimeExecutionService().get_run(model_run_id)

    search_proof = _search_zero_model_proof()
    secret_scan = _credential_persistence_scan()
    runtime_after = _runtime_status()
    database_after = _database_snapshot()
    runtime_count_delta = {
        name: runtime_after["counts"][name] - runtime_before["counts"][name]
        for name in runtime_before["counts"]
    }

    consent_evidence = {
        "version": "v10-c-user-consent-supplement-1",
        "evidence_timing": "post_call_supplemental",
        "source": "codex_thread_user_message",
        "exact_user_utterance": USER_CONSENT_UTTERANCE,
        "authorization_preceded_dispatch": True,
        "normalized_disclosure": EXTERNAL_SEND_DISCLOSURE,
        "normalized_disclosure_digest": EXTERNAL_SEND_DISCLOSURE_DIGEST,
        "bound_original_artifacts": {
            "report_sha256": original["report"]["sha256"],
            "sentry_sha256": original["sentry"]["sha256"],
            "readiness_digest": sentry.get("readiness_digest"),
            "health_attestation_digest": sentry.get(
                "health_attestation_digest"
            ),
            "provider_selection_digest": sentry.get(
                "provider_selection_digest"
            ),
            "objective_digest": sentry.get("objective_digest"),
            "context_digest": sentry.get("context_digest"),
            "request_digest": sentry.get("request_digest"),
            "reservation_id": sentry.get("reservation_id"),
            "bridge_digest": sentry.get("bridge_digest"),
            "runtime_model_run_id": sentry.get("runtime_model_run_id"),
        },
        "original_runtime_source_anchor_bound_consent": False,
        "future_runtime_contract_requires_typed_consent": True,
        "limitation": (
            "The user authorization preceded dispatch, but the historical "
            "Runtime request did not bind a consent digest. This supplemental "
            "record is honest post-call evidence and does not rewrite history."
        ),
    }
    consent_evidence["consent_evidence_digest"] = canonical_digest(
        consent_evidence
    )

    cost_evidence = {
        "provider_price_metadata": "unknown",
        "actual_cost_microunits": None,
        "zero_cost_claimed": False,
        "accounting_status": "unknown_reserved_ceiling",
        "reserved_ceiling_microunits": (
            ORIGINAL_COST_RESERVATION_CEILING_MICROUNITS
        ),
        "reservation_id": sentry.get("reservation_id"),
        "within_pre_dispatch_budget_by_reservation": True,
        "historical_settlement_limitation": (
            "The original runner passed zero into the old settlement API. "
            "This supplement corrects the interpretation to unknown and "
            "retains the full pre-dispatch ceiling; it does not assert a price."
        ),
    }

    checks = {
        "original_report_go": report.get("status") == "GO",
        "original_report_digest_valid": original["report"][
            "canonical_digest_valid"
        ],
        "original_response_hash_valid": original["cross_binding"][
            "response_hash_valid"
        ],
        "original_artifact_bindings_match": all(
            original["cross_binding"][name]
            for name in (
                "model_run_matches_sentry",
                "objective_digest_matches",
                "context_digest_matches",
            )
        ),
        "runtime_readback_matches": (
            readback.get("model_run_id") == model_run_id
            and readback.get("receipt_identity")
            == report.get("runtime", {}).get("receipt_identity")
        ),
        "no_provider_call_during_supplement": all(
            value == 0 for value in runtime_count_delta.values()
        ),
        "fixed_loopback_search": (
            search_proof.get("local_only") is True
            and search_proof.get("local_endpoint") == SEARCH_LOCAL_ENDPOINT
            and search_proof.get("http_status") == 200
        ),
        "search_zero_model": all(
            value == 0
            for value in search_proof.get("zero_model_proof", {}).values()
        ),
        "exact_credential_not_persisted": secret_scan["passed"],
        "unknown_cost_not_zero": (
            cost_evidence["actual_cost_microunits"] is None
            and cost_evidence["zero_cost_claimed"] is False
        ),
        "user_consent_evidence_recorded": (
            consent_evidence["exact_user_utterance"] == "授权，继续"
            and consent_evidence["authorization_preceded_dispatch"] is True
        ),
        "four_non_runtime_databases_unchanged": all(
            database_before[name]["database"]["sha256"]
            == database_after[name]["database"]["sha256"]
            and database_before[name]["database"]["size"]
            == database_after[name]["database"]["size"]
            for name in ("main", "profile", "semantic", "operational")
        ),
        "runtime_integrity_ok": (
            runtime_after["integrity_check"] == "ok"
            and runtime_after["quick_check"] == "ok"
            and runtime_after["foreign_key_violations"] == 0
        ),
    }
    supplement = {
        "version": SUPPLEMENT_VERSION,
        "status": "GO" if all(checks.values()) else "NO_GO",
        "created_at": _utc_now(),
        "external_provider_calls": 0,
        "original_artifacts_modified": False,
        "checks": checks,
        "original_artifacts": original,
        "user_consent_evidence": consent_evidence,
        "cost_evidence": cost_evidence,
        "search_proof": search_proof,
        "secret_persistence_scan": secret_scan,
        "runtime": {
            "counts_before": runtime_before["counts"],
            "counts_after": runtime_after["counts"],
            "count_delta": runtime_count_delta,
            "readback_model_run_id": readback.get("model_run_id"),
            "readback_receipt_identity": readback.get("receipt_identity"),
        },
        "databases": {
            "before": database_before,
            "after": database_after,
        },
        "historical_limitations": [
            "Original Runtime request did not bind an external consent digest.",
            "Original settlement API was called with zero while price was unknown.",
            "Original search proof digest did not bind the loopback endpoint.",
            "This supplement does not rerun or alter the Provider call.",
        ],
    }
    supplement["supplement_digest"] = canonical_digest(supplement)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with SUPPLEMENT_PATH.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            supplement,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")
    if supplement["status"] != "GO":
        raise PostCallVerificationError("post_call_verification_no_go")
    return supplement


def main() -> int:
    try:
        result = run()
    except PostCallVerificationError as exc:
        print(
            json.dumps(
                {"status": "error", "reason_code": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "supplement_digest": result["supplement_digest"],
                "external_provider_calls": 0,
                "search_endpoint": result["search_proof"]["local_endpoint"],
                "credential_match_count": result[
                    "secret_persistence_scan"
                ]["exact_credential_match_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
