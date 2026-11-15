"""Canonical, fail-closed inventory contract for the production asset root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping
import unicodedata

from memory.db_path_policy import is_reparse_point
from memory.ingestion_core import canonical_json_bytes, sanitize_reason_code


ASSET_BASELINE_SCHEMA_VERSION = "v8.3-asset-baseline-1"
ASSET_BASELINE_RELATIVE_ROOT = "data/ingestion_assets"
PATH_NORMALIZATION_POLICY = "unicode_nfc_posix_relative"
CASE_POLICY = "preserve_case_reject_casefold_collision"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ROOT_STATES = frozenset({"absent", "empty", "populated"})
_BASELINE_FIELDS = frozenset({
    "schema_version",
    "relative_root",
    "root_state",
    "path_normalization",
    "case_policy",
    "inventory_count",
    "physical_bytes",
    "inventory",
    "inventory_digest",
    "baseline_digest",
})
_ENTRY_FIELDS = frozenset({
    "relative_path",
    "size",
    "sha256",
    "entry_type",
})


class AssetBaselineError(RuntimeError):
    """Privacy-safe asset-baseline failure containing a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AssetBaselineError(reason)
    return value


def _nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssetBaselineError(reason)
    return value


def _canonical_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AssetBaselineError("asset_relative_path_invalid")
    if "\\" in value:
        raise AssetBaselineError("asset_relative_path_not_canonical")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or value.startswith("/") or value.endswith("/"):
        raise AssetBaselineError("asset_relative_path_not_canonical")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AssetBaselineError("asset_relative_path_invalid")
    if ":" in parts[0]:
        raise AssetBaselineError("asset_relative_path_invalid")
    return value


def _reject_unsafe_root(root: Path, project_root: Path) -> tuple[Path, Path]:
    raw = os.fspath(root)
    normalized_raw = raw.replace("/", "\\")
    if (
        normalized_raw.startswith("\\\\")
        or normalized_raw.startswith("\\\\?\\")
        or normalized_raw.startswith("\\\\.\\")
    ):
        raise AssetBaselineError("asset_root_device_or_unc_forbidden")
    project = project_root.resolve(strict=True)
    candidate = root if root.is_absolute() else project / root
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(project)
    except ValueError:
        raise AssetBaselineError("asset_root_outside_project") from None
    expected = Path(*ASSET_BASELINE_RELATIVE_ROOT.split("/"))
    if relative != expected:
        raise AssetBaselineError("asset_root_path_mismatch")
    current = project
    for part in relative.parts:
        current = current / part
        if not current.exists():
            break
        if current.is_symlink() or is_reparse_point(current):
            raise AssetBaselineError("asset_root_reparse_forbidden")
    return project, candidate


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda item: (
                    unicodedata.normalize("NFC", item.name).casefold(),
                    unicodedata.normalize("NFC", item.name),
                ),
            )
        except OSError:
            raise AssetBaselineError("asset_inventory_unavailable") from None
        child_directories: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or is_reparse_point(path):
                raise AssetBaselineError("asset_root_reparse_forbidden")
            if entry.is_dir(follow_symlinks=False):
                child_directories.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise AssetBaselineError("asset_entry_type_forbidden")
            relative = unicodedata.normalize(
                "NFC",
                path.relative_to(root).as_posix(),
            )
            inventory.append({
                "relative_path": _canonical_relative_path(relative),
                "size": path.stat().st_size,
                "sha256": _hash_file(path),
                "entry_type": "file",
            })
        stack.extend(reversed(child_directories))
    inventory.sort(
        key=lambda item: (
            item["relative_path"].casefold(),
            item["relative_path"],
        )
    )
    folded: set[str] = set()
    for item in inventory:
        key = item["relative_path"].casefold()
        if key in folded:
            raise AssetBaselineError("asset_relative_path_collision")
        folded.add(key)
    return inventory


def build_asset_baseline(
    *,
    root_state: str,
    inventory: list[Mapping[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": ASSET_BASELINE_SCHEMA_VERSION,
        "relative_root": ASSET_BASELINE_RELATIVE_ROOT,
        "root_state": root_state,
        "path_normalization": PATH_NORMALIZATION_POLICY,
        "case_policy": CASE_POLICY,
        "inventory_count": len(inventory),
        "physical_bytes": sum(int(item["size"]) for item in inventory),
        "inventory": [dict(item) for item in inventory],
        "inventory_digest": _digest(inventory),
    }
    document["baseline_digest"] = _digest(document)
    return validate_asset_baseline(document)


def validate_asset_baseline(
    document: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _BASELINE_FIELDS:
        raise AssetBaselineError("asset_baseline_fields_mismatch")
    if document.get("schema_version") != ASSET_BASELINE_SCHEMA_VERSION:
        raise AssetBaselineError("asset_baseline_schema_mismatch")
    if document.get("relative_root") != ASSET_BASELINE_RELATIVE_ROOT:
        raise AssetBaselineError("asset_baseline_root_mismatch")
    if document.get("path_normalization") != PATH_NORMALIZATION_POLICY:
        raise AssetBaselineError("asset_path_normalization_mismatch")
    if document.get("case_policy") != CASE_POLICY:
        raise AssetBaselineError("asset_case_policy_mismatch")
    root_state = document.get("root_state")
    if root_state not in _ROOT_STATES:
        raise AssetBaselineError("asset_root_state_invalid")
    inventory = document.get("inventory")
    if not isinstance(inventory, list):
        raise AssetBaselineError("asset_inventory_invalid")
    canonical_inventory: list[dict[str, Any]] = []
    folded: set[str] = set()
    for entry in inventory:
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_FIELDS:
            raise AssetBaselineError("asset_inventory_entry_invalid")
        relative = _canonical_relative_path(entry.get("relative_path"))
        folded_relative = relative.casefold()
        if folded_relative in folded:
            raise AssetBaselineError("asset_relative_path_collision")
        folded.add(folded_relative)
        size = _nonnegative_int(entry.get("size"), "asset_entry_size_invalid")
        if entry.get("entry_type") != "file":
            raise AssetBaselineError("asset_entry_type_forbidden")
        canonical_inventory.append({
            "relative_path": relative,
            "size": size,
            "sha256": _sha(entry.get("sha256"), "asset_entry_digest_invalid"),
            "entry_type": "file",
        })
    expected_order = sorted(
        canonical_inventory,
        key=lambda item: (
            item["relative_path"].casefold(),
            item["relative_path"],
        ),
    )
    if canonical_inventory != expected_order:
        raise AssetBaselineError("asset_inventory_order_invalid")
    inventory_count = _nonnegative_int(
        document.get("inventory_count"),
        "asset_inventory_count_invalid",
    )
    physical_bytes = _nonnegative_int(
        document.get("physical_bytes"),
        "asset_physical_bytes_invalid",
    )
    if (
        inventory_count != len(canonical_inventory)
        or physical_bytes != sum(item["size"] for item in canonical_inventory)
    ):
        raise AssetBaselineError("asset_inventory_totals_mismatch")
    if root_state in {"absent", "empty"} and canonical_inventory:
        raise AssetBaselineError("asset_root_state_inventory_mismatch")
    if root_state == "populated" and not canonical_inventory:
        raise AssetBaselineError("asset_root_state_inventory_mismatch")
    if _sha(
        document.get("inventory_digest"),
        "asset_inventory_digest_invalid",
    ) != _digest(canonical_inventory):
        raise AssetBaselineError("asset_inventory_digest_mismatch")
    payload = dict(document)
    recorded = _sha(
        payload.pop("baseline_digest", None),
        "asset_baseline_digest_invalid",
    )
    if recorded != _digest(payload):
        raise AssetBaselineError("asset_baseline_digest_mismatch")
    return dict(document)


def capture_asset_baseline(
    root: str | Path,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    _, candidate = _reject_unsafe_root(Path(root), Path(project_root))
    if not candidate.exists():
        return build_asset_baseline(root_state="absent", inventory=[])
    if not candidate.is_dir():
        raise AssetBaselineError("asset_root_not_directory")
    inventory = _inventory(candidate)
    return build_asset_baseline(
        root_state="populated" if inventory else "empty",
        inventory=inventory,
    )


def require_asset_baseline_match(
    approved: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    validated_approved = validate_asset_baseline(approved)
    validated_actual = validate_asset_baseline(actual)
    if validated_approved != validated_actual:
        raise AssetBaselineError("asset_baseline_mismatch")
