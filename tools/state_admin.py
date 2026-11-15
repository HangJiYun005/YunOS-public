from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.operational_state_models import (  # noqa: E402
    AppendPreview,
    AuthorityArtifact,
    OperationalStateError,
    StateEvent,
    canonical_json,
    normalize_timestamp,
)
from memory.operational_state_store import (  # noqa: E402
    OperationalStateStore,
    initialize_operational_database,
)


def _load_object(path: str, label: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise OperationalStateError(f"{label}_missing")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise OperationalStateError(f"{label}_invalid_json") from None
    if not isinstance(value, dict):
        raise OperationalStateError(f"{label}_must_be_object")
    return value


def _load_token(path: str) -> str:
    source = Path(path).expanduser()
    if not source.is_file():
        raise OperationalStateError("lease_token_file_missing")
    token = source.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise OperationalStateError("invalid_lease_token")
    return token


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _store(args: argparse.Namespace) -> OperationalStateStore:
    return OperationalStateStore(
        args.db,
        allow_production_write=bool(getattr(args, "production", False)),
    )


def _command_init(args: argparse.Namespace) -> None:
    target = initialize_operational_database(
        args.db,
        created_at=args.created_at,
        allow_production=args.production,
    )
    store = OperationalStateStore(
        target, allow_production_write=args.production
    )
    _print(store.read_status())


def _command_status(args: argparse.Namespace) -> None:
    _print(_store(args).read_status())


def _command_preview(args: argparse.Namespace) -> None:
    authority = AuthorityArtifact.from_dict(
        _load_object(args.authority, "authority_artifact")
    )
    event = StateEvent.from_dict(_load_object(args.event, "state_event"))
    preview = _store(args).preview_event(authority=authority, event=event)
    _print(preview.to_dict())


def _command_lease_acquire(args: argparse.Namespace) -> None:
    token = _load_token(args.lease_token_file)
    lease = _store(args).acquire_writer_lease(
        owner_id=args.owner_id,
        token=token,
        acquired_at=args.acquired_at,
        expires_at=args.expires_at,
    )
    _print(
        {
            "status": "acquired",
            "owner_id": lease.owner_id,
            "lease_token_digest": lease.token_digest,
            "acquired_at": lease.acquired_at,
            "expires_at": lease.expires_at,
        }
    )


def _command_lease_release(args: argparse.Namespace) -> None:
    _store(args).release_writer_lease(
        owner_id=args.owner_id,
        token=_load_token(args.lease_token_file),
        released_at=args.released_at,
    )
    _print(
        {
            "status": "released",
            "owner_id": args.owner_id,
            "released_at": normalize_timestamp(args.released_at),
        }
    )


def _command_lease_recover(args: argparse.Namespace) -> None:
    _print(
        _store(args).recover_expired_writer_lease(
            recovered_at=args.recovered_at,
            authority_digest=args.authority_digest,
        )
    )


def _command_commit(args: argparse.Namespace) -> None:
    authority = AuthorityArtifact.from_dict(
        _load_object(args.authority, "authority_artifact")
    )
    preview = AppendPreview.from_dict(
        _load_object(args.preview, "append_preview")
    )
    if args.confirm_preview_digest != preview.preview_digest:
        raise OperationalStateError("preview_confirmation_mismatch")
    receipt = _store(args).commit_preview(
        authority=authority,
        preview=preview,
        lease_owner_id=args.owner_id,
        lease_token=_load_token(args.lease_token_file),
    )
    _print(receipt.to_dict())


def _command_snapshot(args: argparse.Namespace) -> None:
    authority = (
        AuthorityArtifact.from_dict(
            _load_object(args.authority, "authority_artifact")
        )
        if args.authority
        else None
    )
    _print(
        _store(args).create_snapshot(
            created_at=args.created_at,
            authority=authority,
            lease_owner_id=args.owner_id,
            lease_token=(
                _load_token(args.lease_token_file)
                if args.lease_token_file
                else None
            ),
        )
    )


def _command_rebuild(args: argparse.Namespace) -> None:
    authority = (
        AuthorityArtifact.from_dict(
            _load_object(args.authority, "authority_artifact")
        )
        if args.authority
        else None
    )
    _print(
        _store(args).rebuild_projections(
            snapshot_id=args.snapshot_id,
            authority=authority,
            lease_owner_id=args.owner_id,
            lease_token=(
                _load_token(args.lease_token_file)
                if args.lease_token_file
                else None
            ),
            recorded_at=args.recorded_at,
        )
    )


def _command_backup(args: argparse.Namespace) -> None:
    _print(_store(args).backup_to(args.destination))


def _command_restore(args: argparse.Namespace) -> None:
    restored = OperationalStateStore.restore_from_backup(
        args.source, args.destination
    )
    _print(restored.read_status())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "YunOS v9 controlled local Operational State administration. "
            "Normal Web and mobile modes remain read-only."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--db", required=True)
    init.add_argument("--created-at", required=True)
    init.add_argument("--production", action="store_true")
    init.set_defaults(handler=_command_init)

    status = sub.add_parser("status")
    status.add_argument("--db", required=True)
    status.set_defaults(handler=_command_status)

    preview = sub.add_parser("preview")
    preview.add_argument("--db", required=True)
    preview.add_argument("--authority", required=True)
    preview.add_argument("--event", required=True)
    preview.set_defaults(handler=_command_preview)

    acquire = sub.add_parser("lease-acquire")
    acquire.add_argument("--db", required=True)
    acquire.add_argument("--owner-id", required=True)
    acquire.add_argument("--lease-token-file", required=True)
    acquire.add_argument("--acquired-at", required=True)
    acquire.add_argument("--expires-at", required=True)
    acquire.add_argument("--production", action="store_true")
    acquire.set_defaults(handler=_command_lease_acquire)

    release = sub.add_parser("lease-release")
    release.add_argument("--db", required=True)
    release.add_argument("--owner-id", required=True)
    release.add_argument("--lease-token-file", required=True)
    release.add_argument("--released-at", required=True)
    release.add_argument("--production", action="store_true")
    release.set_defaults(handler=_command_lease_release)

    recover = sub.add_parser("lease-recover")
    recover.add_argument("--db", required=True)
    recover.add_argument("--recovered-at", required=True)
    recover.add_argument("--authority-digest", required=True)
    recover.add_argument("--production", action="store_true")
    recover.set_defaults(handler=_command_lease_recover)

    commit = sub.add_parser("commit")
    commit.add_argument("--db", required=True)
    commit.add_argument("--authority", required=True)
    commit.add_argument("--preview", required=True)
    commit.add_argument("--confirm-preview-digest", required=True)
    commit.add_argument("--owner-id", required=True)
    commit.add_argument("--lease-token-file", required=True)
    commit.add_argument("--production", action="store_true")
    commit.set_defaults(handler=_command_commit)

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--db", required=True)
    snapshot.add_argument("--created-at", required=True)
    snapshot.add_argument("--authority")
    snapshot.add_argument("--owner-id")
    snapshot.add_argument("--lease-token-file")
    snapshot.add_argument("--production", action="store_true")
    snapshot.set_defaults(handler=_command_snapshot)

    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--db", required=True)
    rebuild.add_argument("--snapshot-id")
    rebuild.add_argument("--recorded-at")
    rebuild.add_argument("--authority")
    rebuild.add_argument("--owner-id")
    rebuild.add_argument("--lease-token-file")
    rebuild.add_argument("--production", action="store_true")
    rebuild.set_defaults(handler=_command_rebuild)

    backup = sub.add_parser("backup")
    backup.add_argument("--db", required=True)
    backup.add_argument("--destination", required=True)
    backup.set_defaults(handler=_command_backup)

    restore = sub.add_parser("restore")
    restore.add_argument("--source", required=True)
    restore.add_argument("--destination", required=True)
    restore.set_defaults(handler=_command_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except OperationalStateError as exc:
        print(
            canonical_json(
                {
                    "status": "error",
                    "reason_code": exc.reason_code,
                    "production_writer_default_enabled": False,
                }
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
