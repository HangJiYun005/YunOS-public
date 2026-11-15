from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.profile_authorization_fidelity import (
    AuthorizationFidelityError,
    build_ascii_authorization_manifest,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an ASCII-only v8.0-D2 authorization manifest from a strict UTF-8 "
            "input file. This tool cannot execute a Gate or write a sidecar."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _read_input(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise AuthorizationFidelityError("manifest source must be readable strict UTF-8") from exc
    if "\ufffd" in text:
        raise AuthorizationFidelityError("manifest source contains a replacement character")
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        value: dict = {}
        for key, item in pairs:
            if key in value:
                raise AuthorizationFidelityError("manifest source contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise AuthorizationFidelityError("manifest source JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"gate", "items"}:
        raise AuthorizationFidelityError("manifest source must contain only gate and items")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output.suffix.lower() != ".json" or args.output.exists():
        raise AuthorizationFidelityError(
            "output must be a new .json manifest file; existing files are never overwritten"
        )
    source = _read_input(args.input)
    envelope = build_ascii_authorization_manifest(source["gate"], source["items"])
    # The output deliberately has no code-page-dependent characters.
    args.output.write_bytes(envelope.encode("ascii", errors="strict"))
    summary = json.loads(envelope)
    print(json.dumps({
        "status": "manifest_written",
        "manifest_identity": summary["manifest_identity"],
        "item_text_utf8_sha256": summary["item_text_utf8_sha256"],
        "output_is_ascii": True,
        "sidecar_writes": False,
    }, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
