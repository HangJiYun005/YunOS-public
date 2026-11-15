from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    make_identity,
    validate_identity,
    validate_sha256,
)


CAPABILITY_CONTRACT_VERSION = 1
RISK_CLASSES = ("low", "medium", "high", "critical")
REQUIRED_AUTHORITIES = ("none", "read", "prepare", "explicit_act")
IDEMPOTENCY_MODES = (
    "read_only",
    "idempotent",
    "exactly_once",
    "non_idempotent",
)
AVAILABILITY_STATES = ("available", "unavailable", "disabled", "handoff_only")
EXECUTOR_KINDS = (
    "native",
    "runtime",
    "codex",
    "external_agent",
    "local_broker",
)

_RISK_RANK = {value: index for index, value in enumerate(RISK_CLASSES)}
_AUTHORITY_RANK = {
    value: index for index, value in enumerate(REQUIRED_AUTHORITIES)
}
_MAX_TIMEOUT_MS = 1_200_000
_MAX_OUTPUT_BYTES = 104_857_600
_MAX_ARGUMENT_BYTES = 65_536
_MAX_SCHEMA_BYTES = 65_536
_MAX_SCHEMA_DEPTH = 8
_MAX_SCHEMA_NODES = 256
_MAX_COLLECTION_ITEMS = 256
_MAX_SCHEMA_ARRAY_ITEMS = 2_000

_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_FORBIDDEN_CONTROL_TERMS = frozenset(
    {
        "argv",
        "cmd",
        "command",
        "cross-root",
        "cross_root",
        "delete",
        "download",
        "endpoint",
        "executable",
        "fetch",
        "force-push",
        "force_push",
        "host",
        "hostname",
        "http",
        "https",
        "internet",
        "network",
        "outside-root",
        "outside_root",
        "port",
        "powershell",
        "program",
        "pull",
        "push",
        "query",
        "rebase",
        "remove",
        "reset",
        "rm",
        "script",
        "shell",
        "socket",
        "sql",
        "upload",
        "uri",
        "url",
    }
)
_SECRET_FIELD_TERMS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "env",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_SECRET_LITERAL_PATTERNS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[a-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd|authorization|secret)"
        r"\s*[:=]\s*[^\s\"'},;]{8,}"
    ),
)
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
        "const",
        "description",
    }
)
_JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class CognitiveCapabilityError(OperationalStateError):
    """Fail-closed validation error for the v10-B capability contract."""


def _fail(reason_code: str) -> None:
    raise CognitiveCapabilityError(reason_code)


def _normalize_text(
    value: Any,
    *,
    reason_code: str,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        _fail(reason_code)
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        _fail(reason_code)
    _reject_secret_literal(normalized)
    return normalized


def _require_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    reason_code: str,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _fail(reason_code)
    return value


def _validate_enum(value: Any, allowed: Sequence[str], reason_code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(reason_code)
    return value


def _reject_secret_literal(value: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_LITERAL_PATTERNS):
        _fail("capability_secret_literal_forbidden")


def _term_tokens(value: str) -> set[str]:
    lowered = unicodedata.normalize("NFC", value).lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part]
    tokens = set(parts)
    for start in range(len(parts)):
        for stop in range(start + 2, len(parts) + 1):
            tokens.add("_".join(parts[start:stop]))
            tokens.add("-".join(parts[start:stop]))
    tokens.add(lowered)
    return tokens


def _reject_control_term(value: str, reason_code: str) -> None:
    tokens = _term_tokens(value)
    for forbidden in _FORBIDDEN_CONTROL_TERMS:
        if forbidden in tokens:
            _fail(reason_code)


def _reject_secret_field(value: str) -> None:
    lowered = value.lower()
    if any(
        term == lowered
        or lowered.startswith(f"{term}_")
        or lowered.endswith(f"_{term}")
        for term in _SECRET_FIELD_TERMS
    ):
        _fail("capability_secret_field_forbidden")


def _normalize_strings(
    values: Sequence[str] | Iterable[str],
    *,
    reason_code: str,
    scope: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_COLLECTION_ITEMS:
        _fail("capability_collection_limit_exceeded")
    normalized: set[str] = set()
    for item in materialized:
        text = _normalize_text(item, reason_code=reason_code, maximum=512)
        if scope:
            if _SCOPE_RE.fullmatch(text) is None:
                _fail(reason_code)
            _reject_control_term(text, "capability_scope_forbidden")
        normalized.add(text)
    return tuple(sorted(normalized))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _strict_json_loads(raw: str) -> Any:
    if not isinstance(raw, str):
        _fail("capability_json_must_be_text")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            normalized = unicodedata.normalize("NFC", key)
            if normalized in result:
                _fail("duplicate_capability_json_key")
            result[normalized] = value
        return result

    def reject_constant(_: str) -> None:
        _fail("non_finite_capability_number")

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except CognitiveCapabilityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("invalid_capability_json")


def _schema_type(value: Any) -> str:
    if not isinstance(value, str) or value not in _JSON_TYPES:
        _fail("invalid_capability_schema_type")
    return value


def _value_matches_schema_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, (list, tuple))
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return value is None


def _validate_schema(
    schema: Mapping[str, Any],
    *,
    root: bool,
    depth: int,
    counter: list[int],
) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        _fail("invalid_capability_json_schema")
    if depth > _MAX_SCHEMA_DEPTH:
        _fail("capability_schema_depth_exceeded")
    counter[0] += 1
    if counter[0] > _MAX_SCHEMA_NODES:
        _fail("capability_schema_node_limit_exceeded")
    if not set(schema).issubset(_SCHEMA_KEYS):
        _fail("unsupported_capability_schema_keyword")
    schema_type = _schema_type(schema.get("type"))
    if root and schema_type != "object":
        _fail("capability_schema_root_must_be_object")
    normalized: dict[str, Any] = {"type": schema_type}
    if "description" in schema:
        normalized["description"] = _normalize_text(
            schema["description"],
            reason_code="invalid_capability_schema_description",
            maximum=512,
        )
    for keyword in ("enum", "const"):
        if keyword not in schema:
            continue
        if schema_type in {"object", "array"}:
            _fail("capability_container_schema_enum_forbidden")
        candidate = schema[keyword]
        if keyword == "enum":
            if (
                not isinstance(candidate, (list, tuple))
                or not candidate
                or len(candidate) > _MAX_COLLECTION_ITEMS
            ):
                _fail("invalid_capability_schema_enum")
            values = [_normalize_json_value(item) for item in candidate]
            if not all(
                _value_matches_schema_type(item, schema_type) for item in values
            ):
                _fail("capability_schema_enum_type_mismatch")
            encoded = [canonical_json(item) for item in values]
            if len(encoded) != len(set(encoded)):
                _fail("duplicate_capability_schema_enum")
            normalized[keyword] = values
        else:
            normalized_const = _normalize_json_value(candidate)
            if not _value_matches_schema_type(normalized_const, schema_type):
                _fail("capability_schema_const_type_mismatch")
            normalized[keyword] = normalized_const
    if schema_type == "object":
        allowed = {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "description",
            "enum",
            "const",
        }
        if not set(schema).issubset(allowed):
            _fail("invalid_object_capability_schema")
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, Mapping)
            or not isinstance(required, (list, tuple))
            or schema.get("additionalProperties") is not False
        ):
            _fail("capability_object_schema_must_be_closed")
        property_names: set[str] = set()
        normalized_properties: dict[str, Any] = {}
        for raw_name, child in properties.items():
            name = _normalize_text(
                raw_name,
                reason_code="invalid_capability_schema_property",
                maximum=128,
            )
            _reject_control_term(name, "capability_control_field_forbidden")
            _reject_secret_field(name)
            if name in property_names:
                _fail("duplicate_capability_schema_property")
            property_names.add(name)
            normalized_properties[name] = _validate_schema(
                child,
                root=False,
                depth=depth + 1,
                counter=counter,
            )
        normalized_required = [
            _normalize_text(
                item,
                reason_code="invalid_capability_schema_required",
                maximum=128,
            )
            for item in required
        ]
        if (
            len(normalized_required) != len(set(normalized_required))
            or not set(normalized_required).issubset(property_names)
        ):
            _fail("invalid_capability_schema_required")
        normalized.update(
            {
                "properties": {
                    key: normalized_properties[key]
                    for key in sorted(normalized_properties)
                },
                "required": sorted(normalized_required),
                "additionalProperties": False,
            }
        )
    elif schema_type == "array":
        allowed = {
            "type",
            "items",
            "minItems",
            "maxItems",
            "uniqueItems",
            "description",
            "enum",
            "const",
        }
        if not set(schema).issubset(allowed) or "items" not in schema:
            _fail("invalid_array_capability_schema")
        minimum = _require_int(
            schema.get("minItems", 0),
            minimum=0,
            maximum=_MAX_SCHEMA_ARRAY_ITEMS,
            reason_code="invalid_capability_schema_min_items",
        )
        maximum = _require_int(
            schema.get("maxItems"),
            minimum=0,
            maximum=_MAX_SCHEMA_ARRAY_ITEMS,
            reason_code="invalid_capability_schema_max_items",
        )
        if maximum < minimum:
            _fail("invalid_capability_schema_item_range")
        unique = schema.get("uniqueItems", False)
        if not isinstance(unique, bool):
            _fail("invalid_capability_schema_unique_items")
        normalized.update(
            {
                "items": _validate_schema(
                    schema["items"],
                    root=False,
                    depth=depth + 1,
                    counter=counter,
                ),
                "minItems": minimum,
                "maxItems": maximum,
                "uniqueItems": unique,
            }
        )
    elif schema_type == "string":
        allowed = {
            "type",
            "minLength",
            "maxLength",
            "description",
            "enum",
            "const",
        }
        if not set(schema).issubset(allowed):
            _fail("invalid_string_capability_schema")
        minimum = _require_int(
            schema.get("minLength", 0),
            minimum=0,
            maximum=1_000_000,
            reason_code="invalid_capability_schema_min_length",
        )
        maximum_raw = schema.get("maxLength")
        if maximum_raw is None and "enum" not in schema and "const" not in schema:
            _fail("capability_string_schema_must_be_bounded")
        maximum = (
            max(len(item) for item in schema.get("enum", ("",)))
            if maximum_raw is None
            else _require_int(
                maximum_raw,
                minimum=0,
                maximum=1_000_000,
                reason_code="invalid_capability_schema_max_length",
            )
        )
        if maximum < minimum:
            _fail("invalid_capability_schema_string_range")
        normalized.update({"minLength": minimum, "maxLength": maximum})
    elif schema_type in {"integer", "number"}:
        allowed = {
            "type",
            "minimum",
            "maximum",
            "description",
            "enum",
            "const",
        }
        if not set(schema).issubset(allowed):
            _fail("invalid_number_capability_schema")
        if "minimum" in schema:
            minimum = schema["minimum"]
            if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
                _fail("invalid_capability_schema_minimum")
            normalized["minimum"] = minimum
        if "maximum" in schema:
            maximum = schema["maximum"]
            if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
                _fail("invalid_capability_schema_maximum")
            normalized["maximum"] = maximum
        if (
            "minimum" in normalized
            and "maximum" in normalized
            and normalized["maximum"] < normalized["minimum"]
        ):
            _fail("invalid_capability_schema_number_range")
    else:
        allowed = {"type", "description", "enum", "const"}
        if not set(schema).issubset(allowed):
            _fail("invalid_scalar_capability_schema")
    return normalized


def _normalize_json_value(value: Any) -> Any:
    try:
        normalized = json.loads(canonical_json(value))
    except OperationalStateError as exc:
        raise CognitiveCapabilityError(exc.reason_code) from None
    _scan_value_for_secrets(normalized)
    return normalized


def _scan_value_for_secrets(value: Any) -> None:
    if isinstance(value, str):
        _reject_secret_literal(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_secret_field(key)
            _scan_value_for_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_value_for_secrets(item)


def _normalize_schema(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = _validate_schema(schema, root=True, depth=0, counter=[0])
    if len(canonical_json(normalized).encode("utf-8")) > _MAX_SCHEMA_BYTES:
        _fail("capability_schema_byte_limit_exceeded")
    return _freeze_json(normalized)


def _validate_relative_path(value: str) -> None:
    path = value.replace("\\", "/")
    if (
        not path
        or path.startswith("/")
        or path.startswith("//")
        or _WINDOWS_DRIVE_RE.match(path)
        or ":" in path
    ):
        _fail("capability_path_outside_authorized_root")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail("capability_path_outside_authorized_root")
    if parts[0].lower() == ".git":
        _fail("capability_git_control_path_forbidden")


def _validate_instance(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    schema_type = schema["type"]
    if schema_type == "object":
        if not isinstance(value, Mapping):
            _fail("capability_schema_type_mismatch")
        properties = schema["properties"]
        if set(value) - set(properties):
            _fail("capability_schema_additional_property")
        if set(schema["required"]) - set(value):
            _fail("capability_schema_required_property_missing")
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                _fail("capability_schema_property_not_string")
            _reject_secret_field(key)
            result[key] = _validate_instance(
                value[key],
                properties[key],
                f"{path}.{key}",
            )
        return result
    if schema_type == "array":
        if not isinstance(value, (list, tuple)):
            _fail("capability_schema_type_mismatch")
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            _fail("capability_schema_array_budget_exceeded")
        result = [
            _validate_instance(item, schema["items"], f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
        if schema["uniqueItems"]:
            encoded = [canonical_json(item) for item in result]
            if len(encoded) != len(set(encoded)):
                _fail("capability_schema_array_items_not_unique")
        return result
    if schema_type == "string":
        if not isinstance(value, str):
            _fail("capability_schema_type_mismatch")
        normalized = unicodedata.normalize("NFC", value)
        if "\x00" in normalized or not schema["minLength"] <= len(
            normalized
        ) <= schema["maxLength"]:
            _fail("capability_schema_string_budget_exceeded")
        _reject_secret_literal(normalized)
        field_name = path.rsplit(".", 1)[-1]
        base_field_name = field_name.split("[", 1)[0]
        if (
            base_field_name == "relative_path"
            or base_field_name.endswith("_relative_path")
            or base_field_name in {"changed_paths", "entries"}
        ):
            _validate_relative_path(normalized)
        if base_field_name.endswith("_sha256") and _SHA256_RE.fullmatch(
            normalized
        ) is None:
            _fail("invalid_capability_sha256_value")
        result = normalized
    elif schema_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail("capability_schema_type_mismatch")
        result = value
    elif schema_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail("capability_schema_type_mismatch")
        result = value
    elif schema_type == "boolean":
        if not isinstance(value, bool):
            _fail("capability_schema_type_mismatch")
        result = value
    elif schema_type == "null":
        if value is not None:
            _fail("capability_schema_type_mismatch")
        result = None
    else:
        _fail("invalid_capability_schema_type")
    if "minimum" in schema and result < schema["minimum"]:
        _fail("capability_schema_number_below_minimum")
    if "maximum" in schema and result > schema["maximum"]:
        _fail("capability_schema_number_above_maximum")
    if "enum" in schema and result not in schema["enum"]:
        _fail("capability_schema_enum_mismatch")
    if "const" in schema and result != schema["const"]:
        _fail("capability_schema_const_mismatch")
    return result


@dataclass(frozen=True)
class CapabilityProfile:
    capability_id: str
    display_name: str
    version: int
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    risk_class: str
    reversible: bool
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    required_authority: str
    timeout_ms: int
    max_output_bytes: int
    idempotency: str
    verification_method: tuple[str, ...]
    availability: str
    executor_kind: str
    capability_digest: str = ""

    def __post_init__(self) -> None:
        try:
            capability_id = validate_identity(
                self.capability_id,
                "invalid_capability_id",
            )
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        _reject_control_term(capability_id, "prohibited_capability")
        display_name = _normalize_text(
            self.display_name,
            reason_code="invalid_capability_display_name",
            maximum=128,
        )
        _reject_control_term(display_name, "prohibited_capability")
        version = _require_int(
            self.version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_capability_version",
        )
        input_schema = _normalize_schema(self.input_schema)
        output_schema = _normalize_schema(self.output_schema)
        risk_class = _validate_enum(
            self.risk_class,
            RISK_CLASSES,
            "invalid_capability_risk_class",
        )
        if not isinstance(self.reversible, bool):
            _fail("invalid_capability_reversible")
        read_scopes = _normalize_strings(
            self.read_scopes,
            reason_code="invalid_capability_read_scope",
            scope=True,
        )
        write_scopes = _normalize_strings(
            self.write_scopes,
            reason_code="invalid_capability_write_scope",
            scope=True,
        )
        required_authority = _validate_enum(
            self.required_authority,
            REQUIRED_AUTHORITIES,
            "invalid_capability_required_authority",
        )
        timeout_ms = _require_int(
            self.timeout_ms,
            minimum=1,
            maximum=_MAX_TIMEOUT_MS,
            reason_code="invalid_capability_timeout",
        )
        max_output_bytes = _require_int(
            self.max_output_bytes,
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
            reason_code="invalid_capability_max_output",
        )
        idempotency = _validate_enum(
            self.idempotency,
            IDEMPOTENCY_MODES,
            "invalid_capability_idempotency",
        )
        verification_method = _normalize_strings(
            self.verification_method,
            reason_code="invalid_capability_verification_method",
        )
        availability = _validate_enum(
            self.availability,
            AVAILABILITY_STATES,
            "invalid_capability_availability",
        )
        executor_kind = _validate_enum(
            self.executor_kind,
            EXECUTOR_KINDS,
            "invalid_capability_executor_kind",
        )
        if not read_scopes and not write_scopes:
            _fail("capability_scope_required")
        if write_scopes:
            if (
                required_authority != "explicit_act"
                or risk_class == "low"
                or not self.reversible
                or idempotency not in {"idempotent", "exactly_once"}
                or not verification_method
                or executor_kind not in {"native", "local_broker"}
            ):
                _fail("unsafe_controlled_write_capability")
        else:
            if idempotency != "read_only":
                _fail("readonly_capability_must_be_read_only")
            if required_authority not in {"read", "prepare"}:
                _fail("readonly_capability_requires_read_authority")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "input_schema", input_schema)
        object.__setattr__(self, "output_schema", output_schema)
        object.__setattr__(self, "risk_class", risk_class)
        object.__setattr__(self, "read_scopes", read_scopes)
        object.__setattr__(self, "write_scopes", write_scopes)
        object.__setattr__(self, "required_authority", required_authority)
        object.__setattr__(self, "timeout_ms", timeout_ms)
        object.__setattr__(self, "max_output_bytes", max_output_bytes)
        object.__setattr__(self, "idempotency", idempotency)
        object.__setattr__(
            self,
            "verification_method",
            verification_method,
        )
        object.__setattr__(self, "availability", availability)
        object.__setattr__(self, "executor_kind", executor_kind)
        expected = canonical_digest(self.identity_payload())
        if self.capability_digest:
            try:
                validate_sha256(
                    self.capability_digest,
                    "invalid_capability_digest",
                )
            except OperationalStateError as exc:
                raise CognitiveCapabilityError(exc.reason_code) from None
            if self.capability_digest != expected:
                _fail("capability_digest_mismatch")
        object.__setattr__(self, "capability_digest", expected)

    @property
    def grants_authority(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False

    def identity_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "display_name": self.display_name,
            "version": self.version,
            "input_schema": _thaw_json(self.input_schema),
            "output_schema": _thaw_json(self.output_schema),
            "risk_class": self.risk_class,
            "reversible": self.reversible,
            "read_scopes": list(self.read_scopes),
            "write_scopes": list(self.write_scopes),
            "required_authority": self.required_authority,
            "timeout_ms": self.timeout_ms,
            "max_output_bytes": self.max_output_bytes,
            "idempotency": self.idempotency,
            "verification_method": list(self.verification_method),
            "availability": self.availability,
            "executor_kind": self.executor_kind,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.identity_payload()

    def to_record(self) -> dict[str, Any]:
        return {
            "canonical_capability": self.identity_payload(),
            "capability_digest": self.capability_digest,
        }

    def to_canonical_json(self) -> str:
        return canonical_json(self.identity_payload())

    def validate_input(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _validate_instance(arguments, self.input_schema, "$input")
        if len(canonical_json(normalized).encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            _fail("capability_argument_budget_exceeded")
        return normalized

    def validate_output(self, output: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _validate_instance(output, self.output_schema, "$output")
        if len(canonical_json(normalized).encode("utf-8")) > self.max_output_bytes:
            _fail("capability_output_budget_exceeded")
        return normalized

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityProfile":
        expected = {
            "capability_id",
            "display_name",
            "version",
            "input_schema",
            "output_schema",
            "risk_class",
            "reversible",
            "read_scopes",
            "write_scopes",
            "required_authority",
            "timeout_ms",
            "max_output_bytes",
            "idempotency",
            "verification_method",
            "availability",
            "executor_kind",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_capability_profile")
        payload = dict(value)
        for field_name in (
            "read_scopes",
            "write_scopes",
            "verification_method",
        ):
            if not isinstance(payload[field_name], list):
                _fail("invalid_capability_profile")
            payload[field_name] = tuple(payload[field_name])
        return cls(**payload)

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> "CapabilityProfile":
        if not isinstance(value, Mapping) or set(value) != {
            "canonical_capability",
            "capability_digest",
        }:
            _fail("invalid_capability_record")
        if not isinstance(value["canonical_capability"], Mapping):
            _fail("invalid_capability_record")
        payload = dict(value["canonical_capability"])
        profile = cls.from_dict(payload)
        if profile.capability_digest != value["capability_digest"]:
            _fail("capability_digest_mismatch")
        return profile

    @classmethod
    def from_json(cls, raw: str) -> "CapabilityProfile":
        value = _strict_json_loads(raw)
        if not isinstance(value, Mapping):
            _fail("invalid_capability_profile")
        return cls.from_dict(value)


@dataclass(frozen=True)
class CapabilityRequirement:
    capability_id: str
    capability_version: int
    capability_digest: str
    maximum_risk: str = "low"
    require_reversible: bool = True
    allowed_read_scopes: tuple[str, ...] = ()
    allowed_write_scopes: tuple[str, ...] = ()
    maximum_authority: str = "read"
    maximum_timeout_ms: int = 30_000
    maximum_output_bytes: int = 1_048_576
    allowed_idempotency: tuple[str, ...] = ("read_only",)
    required_verification: tuple[str, ...] = ()
    allowed_executor_kinds: tuple[str, ...] = ("native",)
    require_available: bool = True

    def __post_init__(self) -> None:
        try:
            capability_id = validate_identity(
                self.capability_id,
                "invalid_capability_requirement_id",
            )
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        _reject_control_term(capability_id, "prohibited_capability")
        object.__setattr__(self, "capability_id", capability_id)
        _require_int(
            self.capability_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_capability_requirement_version",
        )
        try:
            validate_sha256(
                self.capability_digest,
                "invalid_capability_requirement_digest",
            )
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        object.__setattr__(
            self,
            "maximum_risk",
            _validate_enum(
                self.maximum_risk,
                RISK_CLASSES,
                "invalid_capability_requirement_risk",
            ),
        )
        if not isinstance(self.require_reversible, bool):
            _fail("invalid_capability_requirement_reversible")
        object.__setattr__(
            self,
            "allowed_read_scopes",
            _normalize_strings(
                self.allowed_read_scopes,
                reason_code="invalid_capability_requirement_read_scope",
                scope=True,
            ),
        )
        object.__setattr__(
            self,
            "allowed_write_scopes",
            _normalize_strings(
                self.allowed_write_scopes,
                reason_code="invalid_capability_requirement_write_scope",
                scope=True,
            ),
        )
        object.__setattr__(
            self,
            "maximum_authority",
            _validate_enum(
                self.maximum_authority,
                REQUIRED_AUTHORITIES,
                "invalid_capability_requirement_authority",
            ),
        )
        _require_int(
            self.maximum_timeout_ms,
            minimum=1,
            maximum=_MAX_TIMEOUT_MS,
            reason_code="invalid_capability_requirement_timeout",
        )
        _require_int(
            self.maximum_output_bytes,
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
            reason_code="invalid_capability_requirement_output",
        )
        allowed_idempotency = _normalize_strings(
            self.allowed_idempotency,
            reason_code="invalid_capability_requirement_idempotency",
        )
        if not allowed_idempotency or not set(allowed_idempotency).issubset(
            IDEMPOTENCY_MODES
        ):
            _fail("invalid_capability_requirement_idempotency")
        object.__setattr__(
            self,
            "allowed_idempotency",
            allowed_idempotency,
        )
        object.__setattr__(
            self,
            "required_verification",
            _normalize_strings(
                self.required_verification,
                reason_code="invalid_capability_requirement_verification",
            ),
        )
        allowed_executors = _normalize_strings(
            self.allowed_executor_kinds,
            reason_code="invalid_capability_requirement_executor",
        )
        if not allowed_executors or not set(allowed_executors).issubset(
            EXECUTOR_KINDS
        ):
            _fail("invalid_capability_requirement_executor")
        object.__setattr__(
            self,
            "allowed_executor_kinds",
            allowed_executors,
        )
        if not isinstance(self.require_available, bool):
            _fail("invalid_capability_requirement_availability")


@dataclass(frozen=True)
class CapabilityFeasibility:
    feasible: bool
    reason_codes: tuple[str, ...]
    profile: CapabilityProfile | None
    registry_grants_authority: bool = False
    execution_allowed: bool = False

    def __post_init__(self) -> None:
        if self.registry_grants_authority or self.execution_allowed:
            _fail("capability_registry_cannot_grant_authority")
        if self.feasible != (not self.reason_codes):
            _fail("invalid_capability_feasibility")


@dataclass(frozen=True)
class CapabilityProposal:
    capability_id: str
    capability_version: int
    capability_digest: str
    typed_arguments: Mapping[str, Any]
    idempotency_key: str
    argument_digest: str = ""

    def __post_init__(self) -> None:
        try:
            validate_identity(
                self.capability_id,
                "invalid_capability_proposal_id",
            )
            validate_identity(
                self.idempotency_key,
                "invalid_capability_idempotency_key",
            )
            validate_sha256(
                self.capability_digest,
                "invalid_capability_proposal_digest",
            )
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        _require_int(
            self.capability_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_capability_proposal_version",
        )
        normalized = _normalize_json_value(self.typed_arguments)
        if not isinstance(normalized, dict):
            _fail("invalid_capability_typed_arguments")
        expected = canonical_digest(normalized)
        if self.argument_digest:
            if not _SHA256_RE.fullmatch(self.argument_digest):
                _fail("invalid_capability_argument_digest")
            if self.argument_digest != expected:
                _fail("capability_argument_digest_mismatch")
        object.__setattr__(self, "typed_arguments", _freeze_json(normalized))
        object.__setattr__(self, "argument_digest", expected)

    @property
    def grants_authority(self) -> bool:
        return False

    @property
    def execution_allowed(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "capability_digest": self.capability_digest,
            "typed_arguments": _thaw_json(self.typed_arguments),
            "argument_digest": self.argument_digest,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def create(
        cls,
        *,
        profile: CapabilityProfile,
        typed_arguments: Mapping[str, Any],
        proposal_binding: str,
    ) -> "CapabilityProposal":
        try:
            validate_identity(
                proposal_binding,
                "invalid_capability_proposal_binding",
            )
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        normalized = profile.validate_input(typed_arguments)
        idempotency_key = make_identity(
            "capidem",
            {
                "proposal_binding": proposal_binding,
                "capability_id": profile.capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": normalized,
            },
        )
        return cls(
            capability_id=profile.capability_id,
            capability_version=profile.version,
            capability_digest=profile.capability_digest,
            typed_arguments=normalized,
            idempotency_key=idempotency_key,
        )


class CapabilityRegistry:
    """Immutable descriptor registry; it validates proposals but grants nothing."""

    __slots__ = (
        "_profiles",
        "_by_key",
        "_latest",
        "_registry_digest",
        "_sealed",
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            _fail("capability_registry_immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        profiles: Iterable[CapabilityProfile],
        *,
        expected_registry_digest: str | None = None,
    ) -> None:
        if isinstance(profiles, (str, bytes, Mapping)):
            _fail("invalid_capability_registry_profiles")
        materialized = list(profiles)
        keys: dict[tuple[str, int], CapabilityProfile] = {}
        digests: dict[str, tuple[str, int]] = {}
        for profile in materialized:
            if not isinstance(profile, CapabilityProfile):
                _fail("invalid_capability_registry_profile")
            key = (profile.capability_id, profile.version)
            previous = keys.get(key)
            if previous is not None:
                if previous.capability_digest == profile.capability_digest:
                    _fail("duplicate_capability_profile")
                _fail("capability_profile_drift")
            previous_key = digests.get(profile.capability_digest)
            if previous_key is not None and previous_key != key:
                _fail("capability_digest_replay")
            keys[key] = profile
            digests[profile.capability_digest] = key
        self._profiles = tuple(
            sorted(
                materialized,
                key=lambda item: (item.capability_id, item.version),
            )
        )
        self._by_key = MappingProxyType(dict(keys))
        latest: dict[str, CapabilityProfile] = {}
        for profile in self._profiles:
            latest[profile.capability_id] = profile
        self._latest = MappingProxyType(latest)
        self._registry_digest = canonical_digest(
            {
                "contract_version": CAPABILITY_CONTRACT_VERSION,
                "profiles": [
                    {
                        "capability_id": profile.capability_id,
                        "version": profile.version,
                        "capability_digest": profile.capability_digest,
                    }
                    for profile in self._profiles
                ],
            }
        )
        if expected_registry_digest is not None:
            try:
                validate_sha256(
                    expected_registry_digest,
                    "invalid_capability_registry_digest",
                )
            except OperationalStateError as exc:
                raise CognitiveCapabilityError(exc.reason_code) from None
            if expected_registry_digest != self._registry_digest:
                _fail("capability_registry_digest_mismatch")
        object.__setattr__(self, "_sealed", True)

    @property
    def profiles(self) -> tuple[CapabilityProfile, ...]:
        return self._profiles

    @property
    def registry_digest(self) -> str:
        return self._registry_digest

    @property
    def grants_authority(self) -> bool:
        return False

    @property
    def can_execute(self) -> bool:
        return False

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "contract_version": CAPABILITY_CONTRACT_VERSION,
            "profiles": [profile.to_record() for profile in self._profiles],
            "registry_digest": self.registry_digest,
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> "CapabilityRegistry":
        if not isinstance(value, Mapping) or set(value) != {
            "contract_version",
            "profiles",
            "registry_digest",
        }:
            _fail("invalid_capability_registry_snapshot")
        if value["contract_version"] != CAPABILITY_CONTRACT_VERSION:
            _fail("unsupported_capability_contract_version")
        if not isinstance(value["profiles"], list):
            _fail("invalid_capability_registry_snapshot")
        profiles = [
            CapabilityProfile.from_record(item) for item in value["profiles"]
        ]
        return cls(
            profiles,
            expected_registry_digest=value["registry_digest"],
        )

    def lookup(
        self,
        capability_id: str,
        *,
        version: int | None = None,
        expected_digest: str | None = None,
    ) -> CapabilityProfile:
        try:
            validate_identity(capability_id, "invalid_capability_lookup_id")
        except OperationalStateError as exc:
            raise CognitiveCapabilityError(exc.reason_code) from None
        profile = (
            self._latest.get(capability_id)
            if version is None
            else self._by_key.get((capability_id, version))
        )
        if profile is None:
            _fail("unknown_capability")
        if expected_digest is not None:
            try:
                validate_sha256(
                    expected_digest,
                    "invalid_capability_lookup_digest",
                )
            except OperationalStateError as exc:
                raise CognitiveCapabilityError(exc.reason_code) from None
            if profile.capability_digest != expected_digest:
                _fail("capability_profile_drift")
        return profile

    def feasibility(
        self,
        requirement: CapabilityRequirement,
    ) -> CapabilityFeasibility:
        if not isinstance(requirement, CapabilityRequirement):
            _fail("invalid_capability_requirement")
        profile = self._by_key.get(
            (
                requirement.capability_id,
                requirement.capability_version,
            )
        )
        if profile is None:
            reason = (
                "capability_version_unavailable"
                if requirement.capability_id in self._latest
                else "unknown_capability"
            )
            return CapabilityFeasibility(
                feasible=False,
                reason_codes=(reason,),
                profile=None,
            )
        reasons: list[str] = []
        if profile.capability_digest != requirement.capability_digest:
            reasons.append("capability_profile_drift")
        if (
            requirement.require_available
            and profile.availability != "available"
        ):
            reasons.append(f"capability_{profile.availability}")
        if _RISK_RANK[profile.risk_class] > _RISK_RANK[requirement.maximum_risk]:
            reasons.append("capability_risk_exceeds_requirement")
        if requirement.require_reversible and not profile.reversible:
            reasons.append("capability_not_reversible")
        if not set(profile.read_scopes).issubset(
            requirement.allowed_read_scopes
        ):
            reasons.append("capability_read_scope_not_allowed")
        if not set(profile.write_scopes).issubset(
            requirement.allowed_write_scopes
        ):
            reasons.append("capability_write_scope_not_allowed")
        if _AUTHORITY_RANK[profile.required_authority] > _AUTHORITY_RANK[
            requirement.maximum_authority
        ]:
            reasons.append("capability_authority_exceeds_requirement")
        if profile.timeout_ms > requirement.maximum_timeout_ms:
            reasons.append("capability_timeout_exceeds_requirement")
        if profile.max_output_bytes > requirement.maximum_output_bytes:
            reasons.append("capability_output_exceeds_requirement")
        if profile.idempotency not in requirement.allowed_idempotency:
            reasons.append("capability_idempotency_not_allowed")
        if not set(requirement.required_verification).issubset(
            profile.verification_method
        ):
            reasons.append("capability_verification_missing")
        if profile.executor_kind not in requirement.allowed_executor_kinds:
            reasons.append("capability_executor_not_allowed")
        return CapabilityFeasibility(
            feasible=not reasons,
            reason_codes=tuple(sorted(set(reasons))),
            profile=profile,
        )

    def validate_proposal(
        self,
        proposal: CapabilityProposal,
        *,
        seen_idempotency_keys: Iterable[str] = (),
    ) -> dict[str, Any]:
        if not isinstance(proposal, CapabilityProposal):
            _fail("invalid_capability_proposal")
        if isinstance(seen_idempotency_keys, (str, bytes, Mapping)):
            _fail("invalid_capability_replay_ledger")
        seen_entries = list(seen_idempotency_keys)
        if len(seen_entries) > _MAX_COLLECTION_ITEMS:
            _fail("capability_replay_ledger_limit_exceeded")
        for item in seen_entries:
            try:
                validate_identity(
                    item,
                    "invalid_capability_replay_ledger_entry",
                )
            except OperationalStateError as exc:
                raise CognitiveCapabilityError(exc.reason_code) from None
        seen = set(seen_entries)
        if len(seen) != len(seen_entries):
            _fail("duplicate_capability_replay_ledger_entry")
        if proposal.idempotency_key in seen:
            _fail("capability_proposal_replay")
        profile = self.lookup(
            proposal.capability_id,
            version=proposal.capability_version,
            expected_digest=proposal.capability_digest,
        )
        if profile.availability != "available":
            _fail(f"capability_{profile.availability}")
        arguments = profile.validate_input(proposal.typed_arguments)
        if canonical_digest(arguments) != proposal.argument_digest:
            _fail("capability_argument_digest_mismatch")
        return {
            "proposal": proposal.to_dict(),
            "profile_digest": profile.capability_digest,
            "typed_arguments": arguments,
            "registry_grants_authority": False,
            "execution_allowed": False,
            "decision": "validated_proposal_only",
        }


def _object_schema(
    properties: Mapping[str, Mapping[str, Any]],
    required: Sequence[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def suggested_capability_profiles(
    availability_by_id: Mapping[str, str] | None = None,
) -> tuple[CapabilityProfile, ...]:
    """Return deterministic descriptors only; no executor or authority is opened."""

    availability = dict(availability_by_id or {})
    known_ids = {
        "project.file.read",
        "project.tree.list",
        "git.status.read",
        "project.file.patch",
    }
    if set(availability) - known_ids:
        _fail("unknown_capability_availability_override")
    for state in availability.values():
        _validate_enum(
            state,
            AVAILABILITY_STATES,
            "invalid_capability_availability",
        )

    def state(capability_id: str) -> str:
        return availability.get(capability_id, "disabled")

    relative_path = {"type": "string", "minLength": 1, "maxLength": 1024}
    sha256 = {"type": "string", "minLength": 64, "maxLength": 64}
    profiles = (
        CapabilityProfile(
            capability_id="project.file.read",
            display_name="Read one authorized project file",
            version=1,
            input_schema=_object_schema(
                {"relative_path": relative_path},
                ("relative_path",),
            ),
            output_schema=_object_schema(
                {
                    "relative_path": relative_path,
                    "content": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 262_144,
                    },
                    "content_sha256": sha256,
                    "truncated": {"type": "boolean"},
                },
                ("relative_path", "content", "content_sha256", "truncated"),
            ),
            risk_class="low",
            reversible=True,
            read_scopes=("project.files",),
            write_scopes=(),
            required_authority="read",
            timeout_ms=5_000,
            max_output_bytes=300_000,
            idempotency="read_only",
            verification_method=(
                "content_sha256",
                "resolved_path_within_authorized_root",
            ),
            availability=state("project.file.read"),
            executor_kind="native",
        ),
        CapabilityProfile(
            capability_id="project.tree.list",
            display_name="List authorized project tree entries",
            version=1,
            input_schema=_object_schema(
                {
                    "relative_path": relative_path,
                    "maximum_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2_000,
                    },
                },
                ("relative_path", "maximum_entries"),
            ),
            output_schema=_object_schema(
                {
                    "entries": {
                        "type": "array",
                        "items": relative_path,
                        "minItems": 0,
                        "maxItems": 2_000,
                        "uniqueItems": True,
                    },
                    "truncated": {"type": "boolean"},
                },
                ("entries", "truncated"),
            ),
            risk_class="low",
            reversible=True,
            read_scopes=("project.files",),
            write_scopes=(),
            required_authority="read",
            timeout_ms=5_000,
            max_output_bytes=200_000,
            idempotency="read_only",
            verification_method=("resolved_path_within_authorized_root",),
            availability=state("project.tree.list"),
            executor_kind="native",
        ),
        CapabilityProfile(
            capability_id="git.status.read",
            display_name="Read bounded Git status",
            version=1,
            input_schema=_object_schema({}, ()),
            output_schema=_object_schema(
                {
                    "head": {"type": "string", "minLength": 1, "maxLength": 128},
                    "branch": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "changed_paths": {
                        "type": "array",
                        "items": relative_path,
                        "minItems": 0,
                        "maxItems": 2_000,
                        "uniqueItems": True,
                    },
                    "truncated": {"type": "boolean"},
                },
                ("head", "branch", "changed_paths", "truncated"),
            ),
            risk_class="low",
            reversible=True,
            read_scopes=("project.git_status",),
            write_scopes=(),
            required_authority="read",
            timeout_ms=10_000,
            max_output_bytes=200_000,
            idempotency="read_only",
            verification_method=("git_status_porcelain_v2",),
            availability=state("git.status.read"),
            executor_kind="native",
        ),
        CapabilityProfile(
            capability_id="project.file.patch",
            display_name="Replace one authorized project file with hash gates",
            version=1,
            input_schema=_object_schema(
                {
                    "relative_path": relative_path,
                    "expected_before_sha256": sha256,
                    "replacement_content": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 262_144,
                    },
                    "expected_after_sha256": sha256,
                },
                (
                    "relative_path",
                    "expected_before_sha256",
                    "replacement_content",
                    "expected_after_sha256",
                ),
            ),
            output_schema=_object_schema(
                {
                    "relative_path": relative_path,
                    "before_sha256": sha256,
                    "after_sha256": sha256,
                    "backup_reference": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 191,
                    },
                    "verification_passed": {"type": "boolean", "const": True},
                },
                (
                    "relative_path",
                    "before_sha256",
                    "after_sha256",
                    "backup_reference",
                    "verification_passed",
                ),
            ),
            risk_class="medium",
            reversible=True,
            read_scopes=("project.files",),
            write_scopes=("project.files",),
            required_authority="explicit_act",
            timeout_ms=10_000,
            max_output_bytes=16_384,
            idempotency="exactly_once",
            verification_method=(
                "backup_receipt",
                "content_sha256",
                "resolved_path_within_authorized_root",
            ),
            availability=state("project.file.patch"),
            executor_kind="local_broker",
        ),
    )
    return tuple(sorted(profiles, key=lambda item: item.capability_id))


def build_suggested_capability_registry(
    availability_by_id: Mapping[str, str] | None = None,
) -> CapabilityRegistry:
    return CapabilityRegistry(
        suggested_capability_profiles(availability_by_id)
    )
