from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

from memory.cognitive_objective import (
    AcceptancePolicy,
    AuthorityRequest,
    ObjectiveContract,
    ObjectiveIntentError,
    build_objective_contract,
    resolve_objective_intent,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    StateEvent,
    canonical_digest,
    validate_sha256,
)


WRITE_COMMANDS = frozenset({"candidate-create", "candidate-decide", "apply"})
READ_COMMANDS = frozenset({"status", "list", "show"})
MAX_INPUT_BYTES = 1_048_576
MAX_TOKEN_FILE_BYTES = 4_096


class ObjectiveAdminError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _read_json(path: str) -> dict[str, Any]:
    if path == "-":
        binary_stdin = getattr(sys.stdin, "buffer", None)
        if binary_stdin is not None:
            raw_bytes = binary_stdin.read(MAX_INPUT_BYTES + 1)
            if len(raw_bytes) > MAX_INPUT_BYTES:
                raise ObjectiveAdminError("objective_input_too_large")
            try:
                raw = raw_bytes.decode("utf-8-sig")
            except UnicodeError:
                raise ObjectiveAdminError(
                    "invalid_objective_input_json"
                ) from None
        else:
            raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
    else:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ObjectiveAdminError("objective_input_not_found")
        try:
            with candidate.open("rb") as stream:
                raw_bytes = stream.read(MAX_INPUT_BYTES + 1)
            if len(raw_bytes) > MAX_INPUT_BYTES:
                raise ObjectiveAdminError("objective_input_too_large")
            raw = raw_bytes.decode("utf-8-sig")
        except ObjectiveAdminError:
            raise
        except (OSError, UnicodeError):
            raise ObjectiveAdminError("invalid_objective_input_json") from None
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ObjectiveAdminError("objective_input_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ObjectiveAdminError("invalid_objective_input_json") from None
    if not isinstance(payload, dict):
        raise ObjectiveAdminError("objective_input_must_be_object")
    return payload


def _read_token(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise ObjectiveAdminError("lease_token_file_not_found")
    try:
        with candidate.open("rb") as stream:
            token_bytes = stream.read(MAX_TOKEN_FILE_BYTES + 1)
        if len(token_bytes) > MAX_TOKEN_FILE_BYTES:
            raise ObjectiveAdminError("lease_token_file_too_large")
        token = token_bytes.decode("utf-8-sig").strip()
    except ObjectiveAdminError:
        raise
    except OSError:
        raise ObjectiveAdminError("invalid_lease_token_file") from None
    except UnicodeError:
        raise ObjectiveAdminError("invalid_lease_token_file") from None
    if len(token.encode("utf-8")) > MAX_TOKEN_FILE_BYTES:
        raise ObjectiveAdminError("lease_token_file_too_large")
    if len(token) < 16:
        raise ObjectiveAdminError("invalid_lease_token")
    return token


def _build_contract(payload: Mapping[str, Any]) -> ObjectiveContract:
    allowed = {
        "user_utterance",
        "project_id",
        "source_anchors",
        "success_criteria",
        "interpreted_goal",
        "agency_level",
        "agency_selected_by",
        "maximum_authorized_agency_level",
        "agent_proposed_agency_level",
        "agency_selection_source_anchor_id",
        "cognitive_stance",
        "prohibited_outcomes",
        "ambiguity_signals",
        "risk_tolerance",
        "time_constraints",
        "resource_constraints",
        "assumptions",
        "context_requirements",
        "acceptance_policy",
        "authority_request",
        "objective_id",
        "version",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ObjectiveAdminError("unsupported_objective_input_field")
    values = dict(payload)
    if isinstance(values.get("acceptance_policy"), Mapping):
        if values["acceptance_policy"].get("automatic_allowed") is True:
            raise ObjectiveAdminError(
                "trusted_acceptance_preauthorization_required"
            )
        values["acceptance_policy"] = AcceptancePolicy.from_dict(
            values["acceptance_policy"]
        )
    if isinstance(values.get("authority_request"), Mapping):
        values["authority_request"] = AuthorityRequest.from_dict(
            values["authority_request"]
        )
    try:
        return build_objective_contract(**values)
    except TypeError:
        raise ObjectiveAdminError("objective_input_fields_mismatch") from None


def _contract_from_value(value: Any) -> ObjectiveContract:
    if not isinstance(value, Mapping):
        raise ObjectiveAdminError("objective_contract_input_must_be_object")
    if "canonical_digest" in value:
        return ObjectiveContract.from_dict(value)
    return _build_contract(value)


def _normalized_write_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    operation = payload.get("operation")
    if operation not in WRITE_COMMANDS:
        raise ObjectiveAdminError("invalid_objective_write_operation")
    if operation == "candidate-create":
        expected = {
            "operation",
            "contract",
            "source_anchor_id",
            "confidence_ppm",
            "created_at",
            "expires_at",
        }
        if set(payload) != expected:
            raise ObjectiveAdminError("invalid_candidate_create_request")
        contract = _contract_from_value(payload["contract"])
        if (
            not isinstance(payload["confidence_ppm"], int)
            or isinstance(payload["confidence_ppm"], bool)
            or not 0 <= payload["confidence_ppm"] <= 1_000_000
        ):
            raise ObjectiveAdminError("invalid_candidate_confidence")
        source_anchor_id = payload["source_anchor_id"]
        anchors = {
            anchor.anchor_id: anchor
            for anchor in contract.source_anchors
        }
        selected = anchors.get(source_anchor_id)
        if selected is None:
            raise ObjectiveAdminError("objective_source_anchor_not_found")
        try:
            source_anchor = SourceAnchor.from_dict(selected.to_dict())
        except OperationalStateError as exc:
            raise ObjectiveAdminError(exc.reason_code) from None
        return {
            "operation": operation,
            "objective_contract": contract.to_dict(),
            "source_anchor": source_anchor.to_dict(),
            "confidence_ppm": payload["confidence_ppm"],
            "created_at": payload["created_at"],
            "expires_at": payload["expires_at"],
        }
    if operation == "candidate-decide":
        expected = {
            "operation",
            "candidate_id",
            "candidate_digest",
            "decision",
            "recorded_at",
        }
        if set(payload) != expected:
            raise ObjectiveAdminError("invalid_candidate_decide_request")
        if payload["decision"] not in {"approve", "reject"}:
            raise ObjectiveAdminError("invalid_candidate_decision")
        try:
            validate_sha256(
                payload["candidate_digest"],
                "invalid_objective_candidate_digest",
            )
        except OperationalStateError as exc:
            raise ObjectiveAdminError(exc.reason_code) from None
        return dict(payload)
    expected = {
        "operation",
        "contract",
        "event",
        "approval_id",
        "created_at",
    }
    if set(payload) != expected:
        raise ObjectiveAdminError("invalid_objective_apply_request")
    contract = _contract_from_value(payload["contract"])
    try:
        event = StateEvent.from_dict(payload["event"])
    except OperationalStateError as exc:
        raise ObjectiveAdminError(exc.reason_code) from None
    approval_id = payload["approval_id"]
    if approval_id is not None and not isinstance(approval_id, str):
        raise ObjectiveAdminError("invalid_objective_approval_id")
    return {
        "operation": operation,
        "objective_contract": contract.to_dict(),
        "event": event.to_dict(),
        "approval_id": approval_id,
        "created_at": payload["created_at"],
    }


def _preview(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "operation" in payload:
        normalized = _normalized_write_request(payload)
    else:
        contract = _build_contract(payload)
        normalized = {
            "operation": "objective-contract",
            "objective_contract": contract.to_dict(),
        }
    preview_payload = {"operation": "objective-preview", "request": normalized}
    return {
        "status": "preview",
        "provider_calls": 0,
        "persisted": False,
        "preview_digest": canonical_digest(preview_payload),
        "normalized_request": normalized,
        "objective_contract": normalized.get("objective_contract"),
    }


def _resolve(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "user_utterance",
        "interpreted_goal",
        "agency_level",
        "agency_selected_by",
        "maximum_authorized_agency_level",
        "agent_proposed_agency_level",
        "cognitive_stance",
        "success_criteria",
        "prohibited_outcomes",
        "ambiguity_signals",
        "project_id",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ObjectiveAdminError("unsupported_objective_resolution_field")
    values = {key: value for key, value in payload.items() if key != "project_id"}
    try:
        resolution = resolve_objective_intent(**values)
    except TypeError:
        raise ObjectiveAdminError("objective_resolution_fields_mismatch") from None
    return {
        "status": "resolved",
        "provider_calls": 0,
        "persisted": False,
        "project_id": payload.get("project_id"),
        "objective_contract": resolution.to_dict(),
    }


def _require_write_confirmation(
    *,
    payload: Mapping[str, Any],
    expected_digest: str,
    command: str,
) -> dict[str, Any]:
    try:
        validate_sha256(expected_digest, "invalid_preview_digest")
    except OperationalStateError as exc:
        raise ObjectiveAdminError(exc.reason_code) from None
    preview = _preview(payload)
    preview_digest = preview["preview_digest"]
    if preview_digest != expected_digest:
        raise ObjectiveAdminError("objective_preview_digest_mismatch")
    if preview["normalized_request"]["operation"] != command:
        raise ObjectiveAdminError("objective_preview_operation_mismatch")
    return preview["normalized_request"]


def _load_state_store(path: str, *, write: bool):
    try:
        from memory.cognitive_state_store import CognitiveStateStore
    except ImportError:
        raise ObjectiveAdminError("cognitive_state_store_unavailable") from None
    return CognitiveStateStore(Path(path).expanduser().resolve())


def _call_first(service: Any, names: tuple[str, ...], **kwargs: Any) -> Any:
    for name in names:
        method = getattr(service, name, None)
        if callable(method):
            return method(**kwargs)
    raise ObjectiveAdminError("cognitive_state_operation_unavailable")


def _run_read_command(args: argparse.Namespace) -> dict[str, Any]:
    service = _load_state_store(args.db, write=False)
    if args.command == "status":
        result = _call_first(service, ("status", "read_status"))
    elif args.command == "list":
        result = _call_first(
            service,
            ("list_objectives",),
            project_id=args.project_id,
            limit=args.limit,
        )
    else:
        result = _call_first(
            service,
            ("get_objective", "show_objective"),
            objective_id=args.objective_id,
            version=args.version,
        )
    payload = dict(result) if isinstance(result, Mapping) else {"result": result}
    payload.setdefault("readonly", True)
    return payload


def _run_write_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.input == "-":
        raise ObjectiveAdminError("objective_write_requires_utf8_input_file")
    payload = _read_json(args.input)
    try:
        authority = AuthorityArtifact.from_dict(_read_json(args.authority))
    except OperationalStateError as exc:
        raise ObjectiveAdminError(exc.reason_code) from None
    request = _require_write_confirmation(
        payload=payload,
        expected_digest=args.preview_digest,
        command=args.command,
    )
    service = _load_state_store(args.db, write=True)
    lease_token = _read_token(args.lease_token_file)
    if args.command == "candidate-create":
        result = service.create_objective_candidate(
            contract=request["objective_contract"],
            source_anchor=SourceAnchor.from_dict(request["source_anchor"]),
            confidence_ppm=request["confidence_ppm"],
            created_at=request["created_at"],
            expires_at=request["expires_at"],
            authority=authority,
            lease_owner_id=args.lease_owner,
            lease_token=lease_token,
        )
    elif args.command == "candidate-decide":
        result = service.decide_objective_candidate(
            candidate_id=request["candidate_id"],
            decision=request["decision"],
            authority=authority,
            recorded_at=request["recorded_at"],
            expected_candidate_digest=request["candidate_digest"],
            lease_owner_id=args.lease_owner,
            lease_token=lease_token,
        )
    else:
        result = service.commit_objective(
            contract=request["objective_contract"],
            authority=authority,
            event=StateEvent.from_dict(request["event"]),
            lease_owner_id=args.lease_owner,
            lease_token=lease_token,
            created_at=request["created_at"],
            approval_id=request["approval_id"],
        )
    response = dict(result) if isinstance(result, Mapping) else {"result": result}
    response["preview_digest"] = args.preview_digest
    return response


def _add_input_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input",
        default="-",
        metavar="JSON_FILE",
        help=(
            "Read JSON from a UTF-8 file. Stdin is supported only for read-only "
            "resolve/preview; Windows PowerShell 5 callers should use a file."
        ),
    )


def _add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, metavar="TEMP_OPERATIONAL_DB")


def _add_write_gate_arguments(parser: argparse.ArgumentParser) -> None:
    _add_db_argument(parser)
    _add_input_argument(parser)
    parser.add_argument("--preview-digest", required=True)
    parser.add_argument("--authority", required=True, metavar="AUTHORITY_JSON")
    parser.add_argument("--lease-owner", required=True)
    parser.add_argument(
        "--lease-token-file",
        required=True,
        metavar="LEASE_TOKEN_FILE",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YunOS v10-A local Objective Contract control utility."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    _add_input_argument(commands.add_parser("resolve"))
    _add_input_argument(commands.add_parser("preview"))

    _add_db_argument(commands.add_parser("status"))
    list_parser = commands.add_parser("list")
    _add_db_argument(list_parser)
    list_parser.add_argument("--project-id")
    list_parser.add_argument("--limit", type=int, default=50)
    show_parser = commands.add_parser("show")
    _add_db_argument(show_parser)
    show_parser.add_argument("--objective-id", required=True)
    show_parser.add_argument("--version", type=int)

    candidate_create = commands.add_parser("candidate-create")
    _add_write_gate_arguments(candidate_create)

    candidate_decide = commands.add_parser("candidate-decide")
    _add_write_gate_arguments(candidate_decide)

    apply_parser = commands.add_parser("apply")
    _add_write_gate_arguments(apply_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            result = _resolve(_read_json(args.input))
        elif args.command == "preview":
            result = _preview(_read_json(args.input))
        elif args.command in READ_COMMANDS:
            result = _run_read_command(args)
        elif args.command in WRITE_COMMANDS:
            result = _run_write_command(args)
        else:
            raise ObjectiveAdminError("unknown_objective_command")
    except (
        ObjectiveAdminError,
        ObjectiveIntentError,
        OperationalStateError,
    ) as exc:
        reason_code = getattr(exc, "reason_code", str(exc))
        print(
            json.dumps(
                {"status": "failed", "reason_code": reason_code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
