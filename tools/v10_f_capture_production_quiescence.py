"""Capture one short-lived, read-only v10-F production quiescence record.

The migration entry point independently rechecks every live gate.  This tool
only creates the canonical operator evidence consumed by the preproduction
backup and fixed control preparation steps.  It accepts no database, port,
Tailscale executable, or output-path override.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_production_migration import (  # noqa: E402
    COGNITIVE_CAPTURE_TAILSCALE_COMMANDS,
    CognitiveProductionMigrationError,
    PRODUCTION_OPERATIONAL_DATABASE,
    PRODUCTION_QUIESCENCE_RECORD,
    _capture_live_quiescence,
    validate_cognitive_quiescence_capture_artifact,
    validate_cognitive_production_quiescence_evidence,
)
from memory.db_path_policy import is_reparse_point  # noqa: E402
from memory.operational_state_models import canonical_json, utc_now  # noqa: E402
from memory.secure_control_files import (  # noqa: E402
    SecureControlFileError,
    pinned_control_directory,
)
from tools.v10_f_prepare_production_migration import (  # noqa: E402
    CAPTURE_CONTROLLER_DIGEST,
    FORMAL_CAPTURE_ROOT,
    TRUSTED_TAILSCALE_EXECUTABLE,
    USER_AUTHORIZATION_TEXT_SHA256,
    ControlPreparationError,
    _temp_only_path,
    build_sealed_quiescence_capture_artifact,
    build_service_quiescence_evidence,
)


CAPTURE_ROOT = FORMAL_CAPTURE_ROOT
TAILSCALE_MAX_OUTPUT_BYTES = 1024 * 1024
TAILSCALE_TIMEOUT_SECONDS = 20
EXPECTED_PROXY = "http://127.0.0.1:8765"


class QuiescenceCaptureError(ValueError):
    """Stable, secret-free capture failure."""


def _fail(reason_code: str) -> None:
    raise QuiescenceCaptureError(reason_code)


def _run_boundary_after_exact_source_match(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    capture_boundary: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Never start the boundary probe until every pinned identity matches."""

    if dict(actual) != dict(expected):
        _fail("capture_source_identity_changed")
    return capture_boundary()


def _reject_reparse_chain(path: Path) -> None:
    current = path.absolute()
    while True:
        try:
            if (current.exists() or current.is_symlink()) and is_reparse_point(current):
                _fail("quiescence_capture_path_forbidden")
        except OSError:
            _fail("quiescence_capture_path_unavailable")
        if current.parent == current:
            return
        current = current.parent


def _resolve_tailscale_executable() -> Path:
    try:
        path = TRUSTED_TAILSCALE_EXECUTABLE.resolve(strict=True)
        _reject_reparse_chain(path)
        if not path.is_file() or is_reparse_point(path):
            _fail("tailscale_cli_path_forbidden")
    except OSError:
        _fail("tailscale_cli_unavailable")
    return path


def _run_tailscale_json(arguments: tuple[str, ...]) -> dict[str, Any]:
    executable = _resolve_tailscale_executable()
    try:
        completed = subprocess.run(
            (str(executable), *arguments),
            check=True,
            capture_output=True,
            timeout=TAILSCALE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("tailscale_status_unavailable")
    if len(completed.stdout) > TAILSCALE_MAX_OUTPUT_BYTES:
        _fail("tailscale_status_output_too_large")
    try:
        value = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("tailscale_status_invalid")
    if not isinstance(value, dict):
        _fail("tailscale_status_invalid")
    return value


def _normalized_key(value: Any) -> str:
    return str(value).replace("_", "").replace("-", "").casefold()


def _reject_funnel_marker(value: Any) -> None:
    """Reject every Funnel marker shape, including ambiguous variants."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = _normalized_key(key)
            if "funnel" in normalized_key:
                _fail("tailscale_funnel_enabled")
            _reject_funnel_marker(item)
    elif isinstance(value, list):
        for item in value:
            _reject_funnel_marker(item)


def _expected_serve_authority(self_state: Mapping[str, Any]) -> str:
    """Derive the only accepted Serve authority from the online node DNS name."""

    dns_name = self_state.get("DNSName")
    if (
        not isinstance(dns_name, str)
        or not dns_name
        or dns_name != dns_name.strip()
        or len(dns_name) > 254
        or not dns_name.endswith(".")
    ):
        _fail("tailscale_dns_name_invalid")
    hostname = dns_name[:-1]
    labels = hostname.split(".")
    if (
        not hostname
        or hostname.endswith(".")
        or len(hostname) > 253
        or not hostname.isascii()
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                not (character.isalnum() or character == "-")
                for character in label
            )
            for label in labels
        )
    ):
        _fail("tailscale_dns_name_invalid")
    return f"{hostname}:443"


def _normalize_unique_serve_route(
    value: Mapping[str, Any],
    *,
    allow_empty: bool,
    expected_authority: str,
) -> dict[str, Any] | None:
    """Accept only the single supported HTTPS 443 -> loopback route."""

    if not isinstance(value, Mapping):
        _fail("tailscale_serve_status_invalid")
    record = dict(value)
    _reject_funnel_marker(record)
    if not record and allow_empty:
        return None
    if set(record) != {"TCP", "Web"}:
        _fail("tailscale_serve_config_not_unique")
    tcp = record.get("TCP")
    web = record.get("Web")
    https_record = tcp.get("443") if isinstance(tcp, Mapping) else None
    https_enabled = (
        https_record.get("HTTPS")
        if isinstance(https_record, Mapping)
        else None
    )
    if (
        not isinstance(tcp, Mapping)
        or set(tcp) != {"443"}
        or not isinstance(https_record, Mapping)
        or set(https_record) != {"HTTPS"}
        or type(https_enabled) is not bool
        or https_enabled is not True
        or not isinstance(web, Mapping)
        or len(web) != 1
    ):
        _fail("tailscale_serve_config_not_unique")
    authority, route = next(iter(web.items()))
    if (
        not isinstance(authority, str)
        or not authority
        or authority != expected_authority
        or not isinstance(route, Mapping)
        or set(route) != {"Handlers"}
    ):
        _fail("tailscale_serve_config_not_unique")
    handlers = route.get("Handlers")
    if (
        not isinstance(handlers, Mapping)
        or set(handlers) != {"/"}
        or not isinstance(handlers.get("/"), Mapping)
        or dict(handlers["/"]) != {"Proxy": EXPECTED_PROXY}
    ):
        _fail("tailscale_serve_config_not_unique")
    return {
        "https_port": 443,
        "authority": authority,
        "handler_path": "/",
        "proxy": EXPECTED_PROXY,
    }


def capture_tailscale_boundary() -> dict[str, Any]:
    """Capture one strict, normalized Serve/Funnel boundary snapshot."""

    (
        status_arguments,
        serve_before_arguments,
        funnel_arguments,
        serve_after_arguments,
    ) = COGNITIVE_CAPTURE_TAILSCALE_COMMANDS
    status = _run_tailscale_json(status_arguments)
    serve_before = _run_tailscale_json(serve_before_arguments)
    funnel = _run_tailscale_json(funnel_arguments)
    serve_after = _run_tailscale_json(serve_after_arguments)
    self_state = status.get("Self")
    online = self_state.get("Online") if isinstance(self_state, Mapping) else None
    if (
        status.get("BackendState") != "Running"
        or not isinstance(self_state, Mapping)
        or type(online) is not bool
        or online is not True
    ):
        _fail("tailscale_not_running_online")
    expected_authority = _expected_serve_authority(self_state)
    normalized_serve = _normalize_unique_serve_route(
        serve_before,
        allow_empty=False,
        expected_authority=expected_authority,
    )
    _normalize_unique_serve_route(
        serve_after,
        allow_empty=False,
        expected_authority=expected_authority,
    )
    # Tailscale 1.98.9 can return the verified tailnet-only ServeConfig for both
    # `serve status --json` and `funnel status --json`.  Treat that representation
    # as Funnel-off only when both documents are byte-canonically identical and
    # neither contains an explicit/ambiguous public-exposure authority marker.
    _reject_funnel_marker(funnel)
    canonical_serve = canonical_json(dict(serve_before))
    if (
        canonical_json(dict(funnel)) != canonical_serve
        or canonical_json(dict(serve_after)) != canonical_serve
    ):
        _fail("tailscale_funnel_enabled")
    normalized = {
        "backend": "Running",
        "serve": normalized_serve,
        "funnel_enabled": False,
        "funnel_status_matches_serve": True,
    }
    return {
        "tailscale_backend": "Running",
        "tailscale_serve_config_digest": hashlib.sha256(
            canonical_json(normalized).encode("utf-8")
        ).hexdigest(),
        "funnel_enabled": False,
    }


def capture_production_quiescence() -> dict[str, Any]:
    """Measure fixed production gates and return the canonical evidence."""

    network = capture_tailscale_boundary()

    try:
        live = _capture_live_quiescence(PRODUCTION_OPERATIONAL_DATABASE)
    except (CognitiveProductionMigrationError, ControlPreparationError) as exc:
        _fail(str(exc))
    try:
        return build_service_quiescence_evidence(
            captured_at=utc_now(),
            tailscale_serve_config_digest=network[
                "tailscale_serve_config_digest"
            ],
            local_service_listener_count=live["local_service_listener_count"],
            writer_lease_count=live["writer_lease_count"],
            operational_wal_size=live["operational_wal_size"],
            production_write_env_enabled=live["production_write_env_enabled"],
            tailscale_backend=network["tailscale_backend"],
            funnel_enabled=network["funnel_enabled"],
        )
    except (CognitiveProductionMigrationError, ControlPreparationError) as exc:
        _fail(str(exc))


def write_capture(
    evidence: Mapping[str, Any],
    *,
    output_root: Path | None = None,
) -> Path:
    """Write raw evidence under system temp only; never publish formally."""

    if output_root is None:
        _fail("shared_capture_output_root_required")
    try:
        root = _temp_only_path(
            output_root, "shared_capture_formal_path_forbidden"
        )
    except ControlPreparationError as exc:
        _fail(str(exc))
    try:
        normalized = validate_cognitive_production_quiescence_evidence(evidence)
    except Exception as exc:
        _fail(str(exc))
    stamp = str(normalized.get("captured_at", "")).replace(":", "").replace(".", "")
    stamp = stamp.replace("-", "")
    if not stamp or any(character not in "0123456789TZ" for character in stamp):
        _fail("quiescence_capture_timestamp_invalid")
    name = f"service_quiescence_evidence_{stamp}.json"
    target = root / name
    payload = (canonical_json(dict(normalized)) + "\n").encode("utf-8")
    try:
        with pinned_control_directory(
            anchor=root.parent,
            path=root,
            create_if_missing=True,
        ) as directory:
            directory.create_file(name, payload)
            directory.verify()
    except SecureControlFileError as exc:
        reason = str(exc)
        if reason == "secure_control_file_exists":
            _fail("quiescence_capture_exists")
        _fail("quiescence_capture_write_failed")
    return target


def _install_fixed_production_capture_executor():
    """Bind genuine probes and the only formal publisher into one executor."""

    fixed_capture_root = CAPTURE_ROOT.absolute()
    fixed_database = PRODUCTION_OPERATIONAL_DATABASE.absolute()
    fixed_claim_capture_record = PRODUCTION_QUIESCENCE_RECORD.absolute()
    fixed_tailscale_executable = TRUSTED_TAILSCALE_EXECUTABLE.absolute()
    real_subprocess_run = subprocess.run
    real_json_loads = json.loads
    real_live_capture = _capture_live_quiescence
    real_clock = utc_now
    real_build_evidence = build_service_quiescence_evidence
    real_build_artifact = build_sealed_quiescence_capture_artifact
    real_validate_capture_artifact = (
        validate_cognitive_quiescence_capture_artifact
    )
    real_run_boundary_after_source_match = (
        _run_boundary_after_exact_source_match
    )
    real_nonce = secrets.token_hex
    fixed_controller_digest = CAPTURE_CONTROLLER_DIGEST
    fixed_user_authorization_digest = USER_AUTHORIZATION_TEXT_SHA256
    fixed_max_output_bytes = TAILSCALE_MAX_OUTPUT_BYTES
    fixed_timeout_seconds = TAILSCALE_TIMEOUT_SECONDS
    fixed_expected_proxy = EXPECTED_PROXY
    fixed_devnull = subprocess.DEVNULL
    fixed_pipe = subprocess.PIPE
    fixed_tailscale_commands = COGNITIVE_CAPTURE_TAILSCALE_COMMANDS
    fixed_project_root = PROJECT_ROOT.resolve(strict=True)
    fixed_source_paths = {
        "capture_controller": Path(__file__).resolve(strict=True),
        "operational_database": fixed_database.resolve(strict=True),
        "tailscale_executable": fixed_tailscale_executable.resolve(
            strict=True
        ),
    }
    fixed_source_limits = {
        "capture_controller": 16 * 1024 * 1024,
        "operational_database": 16 * 1024 * 1024 * 1024,
        "tailscale_executable": 1024 * 1024 * 1024,
    }

    def fixed_reject_funnel(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).replace("_", "").replace("-", "").casefold()
                if "funnel" in normalized:
                    raise QuiescenceCaptureError("tailscale_funnel_enabled")
                fixed_reject_funnel(item)
        elif isinstance(value, list):
            for item in value:
                fixed_reject_funnel(item)

    def fixed_expected_authority(self_state: Mapping[str, Any]) -> str:
        dns_name = self_state.get("DNSName")
        if (
            not isinstance(dns_name, str)
            or not dns_name
            or dns_name != dns_name.strip()
            or len(dns_name) > 254
            or not dns_name.endswith(".")
        ):
            _fail("tailscale_dns_name_invalid")
        hostname = dns_name[:-1]
        labels = hostname.split(".")
        if (
            not hostname
            or hostname.endswith(".")
            or len(hostname) > 253
            or not hostname.isascii()
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    not (character.isalnum() or character == "-")
                    for character in label
                )
                for label in labels
            )
        ):
            _fail("tailscale_dns_name_invalid")
        return f"{hostname}:443"

    def fixed_normalize_serve(
        value: Mapping[str, Any], expected_authority: str
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise QuiescenceCaptureError("tailscale_serve_status_invalid")
        record = dict(value)
        fixed_reject_funnel(record)
        if set(record) != {"TCP", "Web"}:
            raise QuiescenceCaptureError("tailscale_serve_config_not_unique")
        tcp = record.get("TCP")
        web = record.get("Web")
        https_record = tcp.get("443") if isinstance(tcp, Mapping) else None
        https_enabled = (
            https_record.get("HTTPS")
            if isinstance(https_record, Mapping)
            else None
        )
        if (
            not isinstance(tcp, Mapping)
            or set(tcp) != {"443"}
            or not isinstance(https_record, Mapping)
            or set(https_record) != {"HTTPS"}
            or type(https_enabled) is not bool
            or https_enabled is not True
            or not isinstance(web, Mapping)
            or len(web) != 1
        ):
            raise QuiescenceCaptureError("tailscale_serve_config_not_unique")
        authority, route = next(iter(web.items()))
        if (
            not isinstance(authority, str)
            or not authority
            or authority != expected_authority
            or not isinstance(route, Mapping)
            or set(route) != {"Handlers"}
        ):
            raise QuiescenceCaptureError("tailscale_serve_config_not_unique")
        handlers = route.get("Handlers")
        if (
            not isinstance(handlers, Mapping)
            or set(handlers) != {"/"}
            or not isinstance(handlers.get("/"), Mapping)
            or dict(handlers["/"]) != {"Proxy": fixed_expected_proxy}
        ):
            raise QuiescenceCaptureError("tailscale_serve_config_not_unique")
        return {
            "https_port": 443,
            "authority": authority,
            "handler_path": "/",
            "proxy": fixed_expected_proxy,
        }

    def fixed_run_tailscale(arguments: tuple[str, ...]) -> dict[str, Any]:
        try:
            completed = real_subprocess_run(
                (
                    str(fixed_source_paths["tailscale_executable"]),
                    *arguments,
                ),
                check=True,
                shell=False,
                stdin=fixed_devnull,
                stdout=fixed_pipe,
                stderr=fixed_devnull,
                close_fds=True,
                timeout=fixed_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            _fail("tailscale_status_unavailable")
        if len(completed.stdout) > fixed_max_output_bytes:
            _fail("tailscale_status_output_too_large")
        try:
            value = real_json_loads(
                completed.stdout.decode("utf-8", errors="strict")
            )
        except (UnicodeError, json.JSONDecodeError):
            _fail("tailscale_status_invalid")
        if not isinstance(value, dict):
            _fail("tailscale_status_invalid")
        return value

    def fixed_network_capture() -> dict[str, Any]:
        (
            status_arguments,
            serve_before_arguments,
            funnel_arguments,
            serve_after_arguments,
        ) = fixed_tailscale_commands
        status = fixed_run_tailscale(status_arguments)
        serve_before = fixed_run_tailscale(serve_before_arguments)
        funnel = fixed_run_tailscale(funnel_arguments)
        serve_after = fixed_run_tailscale(serve_after_arguments)
        self_state = status.get("Self")
        online = (
            self_state.get("Online")
            if isinstance(self_state, Mapping)
            else None
        )
        if (
            status.get("BackendState") != "Running"
            or not isinstance(self_state, Mapping)
            or type(online) is not bool
            or online is not True
        ):
            _fail("tailscale_not_running_online")
        expected_authority = fixed_expected_authority(self_state)
        normalized_serve = fixed_normalize_serve(
            serve_before, expected_authority
        )
        fixed_normalize_serve(serve_after, expected_authority)
        fixed_reject_funnel(funnel)
        canonical_serve = canonical_json(dict(serve_before))
        if (
            canonical_json(dict(funnel)) != canonical_serve
            or canonical_json(dict(serve_after)) != canonical_serve
        ):
            _fail("tailscale_funnel_enabled")
        normalized = {
            "backend": "Running",
            "serve": normalized_serve,
            "funnel_enabled": False,
            "funnel_status_matches_serve": True,
        }
        return {
            "tailscale_backend": "Running",
            "tailscale_serve_config_digest": hashlib.sha256(
                canonical_json(normalized).encode("utf-8")
            ).hexdigest(),
            "funnel_enabled": False,
        }

    def fixed_open_source_pins(source_lifetime: ExitStack) -> dict[str, Any]:
        source_pins: dict[str, Any] = {}
        for source_id, path in fixed_source_paths.items():
            try:
                path.relative_to(fixed_project_root)
                anchor = fixed_project_root
            except ValueError:
                anchor = Path(path.anchor).resolve(strict=True)
            directory = source_lifetime.enter_context(
                pinned_control_directory(
                    anchor=anchor,
                    path=path.parent,
                    create_if_missing=False,
                )
            )
            source_pins[source_id] = directory.open_file(
                path.name, maximum_bytes=fixed_source_limits[source_id]
            )
        return source_pins

    def fixed_source_identities_from_pins(
        source_pins: Mapping[str, Any],
    ) -> dict[str, Any]:
        identities: dict[str, Any] = {}
        for source_id in sorted(fixed_source_paths):
            pinned = source_pins[source_id]
            pinned.verify()
            details = os.fstat(pinned.handle.fileno())
            resolved = pinned.path.resolve(strict=True)
            if source_id == "tailscale_executable":
                path_scope = "external-path-digest"
                path_reference = hashlib.sha256(
                    os.path.normcase(
                        os.path.abspath(str(resolved))
                    ).encode("utf-8")
                ).hexdigest()
            else:
                path_scope = "project"
                try:
                    path_reference = resolved.relative_to(
                        fixed_project_root
                    ).as_posix()
                except ValueError:
                    _fail("capture_source_identity_forbidden")
            identities[source_id] = {
                "source_id": source_id,
                "path_scope": path_scope,
                "path_reference": path_reference,
                "device": int(details.st_dev),
                "inode": int(details.st_ino),
                "size": int(details.st_size),
                "mtime_ns": int(details.st_mtime_ns),
                "sha256": pinned.expected_sha256,
            }
        return identities

    def fixed_expected_claim_source_identities(
        source_lifetime: ExitStack,
    ) -> dict[str, Any]:
        directory = source_lifetime.enter_context(
            pinned_control_directory(
                anchor=fixed_project_root,
                path=fixed_claim_capture_record.parent,
                create_if_missing=False,
            )
        )
        pinned = directory.open_file(
            fixed_claim_capture_record.name,
            maximum_bytes=4 * 1024 * 1024,
        )
        try:
            raw_text = pinned.read(4 * 1024 * 1024).decode(
                "utf-8", errors="strict"
            )
            raw = real_json_loads(raw_text)
            if (
                not isinstance(raw, dict)
                or raw_text.strip() != canonical_json(raw)
            ):
                _fail("claim_capture_artifact_invalid")
            artifact = real_validate_capture_artifact(raw)
        except (
            UnicodeError,
            json.JSONDecodeError,
            CognitiveProductionMigrationError,
        ):
            _fail("claim_capture_artifact_invalid")
        return dict(artifact["issuer"]["source_identities"])

    def fixed_claim_snapshot_under_pins() -> dict[str, Any]:
        with ExitStack() as source_lifetime:
            source_pins = fixed_open_source_pins(source_lifetime)
            expected_sources = fixed_expected_claim_source_identities(
                source_lifetime
            )
            before_sources = fixed_source_identities_from_pins(source_pins)
            boundary = real_run_boundary_after_source_match(
                expected_sources,
                before_sources,
                fixed_network_capture,
            )
            after_sources = fixed_source_identities_from_pins(source_pins)
            if after_sources != before_sources:
                _fail("capture_source_identity_changed")
            return {
                "boundary": boundary,
                "source_identities": after_sources,
            }

    def fixed_capture_under_pins() -> dict[str, Any]:
        with ExitStack() as source_lifetime:
            output_directory = source_lifetime.enter_context(
                pinned_control_directory(
                    anchor=fixed_project_root,
                    path=fixed_capture_root,
                    create_if_missing=True,
                )
            )
            source_pins = fixed_open_source_pins(source_lifetime)
            before_sources = fixed_source_identities_from_pins(source_pins)
            network = fixed_network_capture()
            try:
                live = real_live_capture(fixed_database)
                evidence = real_build_evidence(
                    captured_at=real_clock(),
                    tailscale_serve_config_digest=network[
                        "tailscale_serve_config_digest"
                    ],
                    local_service_listener_count=live[
                        "local_service_listener_count"
                    ],
                    writer_lease_count=live["writer_lease_count"],
                    operational_wal_size=live["operational_wal_size"],
                    production_write_env_enabled=live[
                        "production_write_env_enabled"
                    ],
                    tailscale_backend=network["tailscale_backend"],
                    funnel_enabled=network["funnel_enabled"],
                )
            except (CognitiveProductionMigrationError, ControlPreparationError) as exc:
                _fail(str(exc))
            after_sources = fixed_source_identities_from_pins(source_pins)
            if after_sources != before_sources:
                _fail("capture_source_identity_changed")
            try:
                artifact = real_build_artifact(
                    evidence=evidence,
                    source_identities=after_sources,
                    nonce=real_nonce(16),
                )
            except ControlPreparationError as exc:
                _fail(str(exc))
            if (
                artifact["issuer"]["controller_digest"]
                != fixed_controller_digest
                or artifact["issuer"]["user_authorization_text_digest"]
                != fixed_user_authorization_digest
            ):
                _fail("capture_artifact_issuer_invalid")

            stamp = evidence["captured_at"].replace(":", "").replace(
                ".", ""
            ).replace("-", "")
            name = f"service_quiescence_evidence_{stamp}.json"
            target = fixed_capture_root / name
            payload = (canonical_json(artifact) + "\n").encode("utf-8")
            try:
                output_directory.create_file(name, payload)
                output_directory.verify()
            except SecureControlFileError as exc:
                if str(exc) == "secure_control_file_exists":
                    _fail("quiescence_capture_exists")
                _fail("quiescence_capture_identity_changed")
            return {
                "artifact": artifact,
                "evidence": evidence,
                "path": target,
            }

    class _FixedProductionCaptureExecutor:
        __slots__ = ()

        def run(self) -> dict[str, Any]:
            # Keep the genuine probe and clock visibly sealed in this exact
            # executor closure; fixed_capture_under_pins consumes the same
            # objects while source handles remain live.
            _sealed_dependencies = (real_live_capture, real_clock)
            if type(self) is not _FixedProductionCaptureExecutor:
                _fail("exact_production_capture_executor_required")
            if os.name != "nt":
                _fail("production_platform_unsupported")
            try:
                result = fixed_capture_under_pins()
                if not _sealed_dependencies:
                    _fail("capture_dependencies_unavailable")
                return result
            except SecureControlFileError as exc:
                _fail(str(exc))
            except OSError:
                _fail("capture_source_identity_unavailable")

        def claim_snapshot(self) -> dict[str, Any]:
            _sealed_dependencies = (
                real_subprocess_run,
                fixed_tailscale_commands,
            )
            if type(self) is not _FixedProductionCaptureExecutor:
                _fail("exact_production_capture_executor_required")
            if os.name != "nt":
                _fail("production_platform_unsupported")
            try:
                result = fixed_claim_snapshot_under_pins()
                if not _sealed_dependencies:
                    _fail("capture_dependencies_unavailable")
                return result
            except SecureControlFileError as exc:
                _fail(str(exc))
            except OSError:
                _fail("capture_source_identity_unavailable")

    return _FixedProductionCaptureExecutor()


_FIXED_PRODUCTION_CAPTURE_EXECUTOR = _install_fixed_production_capture_executor()
del _install_fixed_production_capture_executor


def capture_fixed_production_quiescence() -> dict[str, Any]:
    """Non-injectable formal entry with code-owned probes, clock and root."""

    return _FIXED_PRODUCTION_CAPTURE_EXECUTOR.run()


def capture_fixed_claim_boundary_and_source_identities() -> dict[str, Any]:
    """Return one sealed claim snapshot while all three sources stay pinned."""

    return _FIXED_PRODUCTION_CAPTURE_EXECUTOR.claim_snapshot()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture fixed, read-only YunOS v10-F quiescence evidence"
    )
    parser.add_argument("--capture", action="store_true", required=True)
    parser.add_argument("--confirm-service-stopped", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    try:
        result = capture_fixed_production_quiescence()
        evidence = result["evidence"]
        path = result["path"]
    except (ControlPreparationError, QuiescenceCaptureError) as exc:
        print(canonical_json({"status": "failed", "reason_code": str(exc)}))
        return 1
    print(
        canonical_json(
            {
                "captured_at": evidence["captured_at"],
                "evidence_digest": evidence["evidence_digest"],
                "path": str(path),
                "status": "captured",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
