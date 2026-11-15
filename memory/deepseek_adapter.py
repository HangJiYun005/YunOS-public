"""Single-provider, non-streaming DeepSeek transport boundary for YunOS v8.0-B/C."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROVIDER_ID = "deepseek"
MODEL_ID = "deepseek-chat"
API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_KEY_FILE = Path(__file__).resolve().parents[1] / "config_private" / "deepseek.env"
_ARTIFACT_RE = re.compile(r"rqa_[0-9a-f]{64}")
_ARTIFACT_FIELDS = {
    "protocol_version", "artifact_version", "artifact_identity", "delivery_binding",
    "approved_profile_binding", "system_instruction", "provider", "model_config",
    "payload", "wire_body_base64", "wire_body_sha256",
}


class DeepSeekProviderError(RuntimeError):
    """Sanitized provider failure safe for the runtime ledger and API."""

    def __init__(
        self, code: str, *, category: str, retryable: bool, http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.http_status = http_status


@dataclass(frozen=True)
class DeepSeekResponse:
    provider_response_id: str
    model: str
    content: str
    usage: dict[str, int]
    finish_reason: str


@dataclass(frozen=True)
class PreparedDeepSeekRequest:
    """Immutable, integrity-checked request that carries the exact HTTP bytes."""

    artifact_identity: str
    artifact_canonical_bytes: bytes
    wire_body_sha256: str
    wire_body: bytes


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise DeepSeekProviderError(
            "request_artifact_invalid", category="request", retryable=False,
        ) from None


def render_deepseek_wire_body(payload: dict[str, Any]) -> bytes:
    """Render the provider body once using the canonical wire representation."""
    if not isinstance(payload, dict):
        raise DeepSeekProviderError("request_payload_invalid", category="request", retryable=False)
    return _canonical_bytes(payload)


def prepare_deepseek_request(request_artifact: Any) -> PreparedDeepSeekRequest:
    """Validate the complete artifact and freeze its already-rendered HTTP bytes."""
    if not isinstance(request_artifact, dict) or set(request_artifact) != _ARTIFACT_FIELDS:
        raise DeepSeekProviderError("request_artifact_invalid", category="request", retryable=False)
    identity = request_artifact.get("artifact_identity")
    if not isinstance(identity, str) or not _ARTIFACT_RE.fullmatch(identity):
        raise DeepSeekProviderError("request_artifact_invalid", category="request", retryable=False)
    identity_payload = {
        key: value for key, value in request_artifact.items() if key != "artifact_identity"
    }
    expected_identity = "rqa_" + hashlib.sha256(_canonical_bytes(identity_payload)).hexdigest()
    if identity != expected_identity:
        raise DeepSeekProviderError("request_artifact_invalid", category="request", retryable=False)
    encoded = request_artifact.get("wire_body_base64")
    wire_hash = request_artifact.get("wire_body_sha256")
    if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded):
        raise DeepSeekProviderError("request_wire_body_invalid", category="request", retryable=False)
    try:
        wire_body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise DeepSeekProviderError(
            "request_wire_body_invalid", category="request", retryable=False,
        ) from None
    if not wire_body or wire_hash != hashlib.sha256(wire_body).hexdigest():
        raise DeepSeekProviderError("request_wire_body_invalid", category="request", retryable=False)
    try:
        rendered_payload = json.loads(wire_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeepSeekProviderError(
            "request_wire_body_invalid", category="request", retryable=False,
        ) from None
    if _canonical_bytes(rendered_payload) != wire_body:
        raise DeepSeekProviderError("request_wire_body_invalid", category="request", retryable=False)
    payload = request_artifact.get("payload")
    if rendered_payload != payload:
        raise DeepSeekProviderError("request_wire_body_invalid", category="request", retryable=False)
    if (
        not isinstance(payload, dict)
        or payload.get("model") != MODEL_ID
        or payload.get("stream") is not False
        or not isinstance(payload.get("messages"), list)
        or len(payload["messages"]) != 2
        or [item.get("role") for item in payload["messages"]] != ["system", "user"]
        or not all(
            isinstance(item.get("content"), str) and item["content"]
            for item in payload["messages"]
        )
    ):
        raise DeepSeekProviderError("request_payload_invalid", category="request", retryable=False)
    return PreparedDeepSeekRequest(
        artifact_identity=identity,
        artifact_canonical_bytes=_canonical_bytes(request_artifact),
        wire_body_sha256=wire_hash,
        wire_body=wire_body,
    )


def _validate_prepared_request(value: Any) -> PreparedDeepSeekRequest:
    if not isinstance(value, PreparedDeepSeekRequest):
        raise DeepSeekProviderError("request_artifact_invalid", category="request", retryable=False)
    try:
        artifact = json.loads(value.artifact_canonical_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeepSeekProviderError(
            "request_artifact_invalid", category="request", retryable=False,
        ) from None
    prepared = prepare_deepseek_request(artifact)
    if prepared != value:
        raise DeepSeekProviderError("request_artifact_invalid", category="request", retryable=False)
    return value


def load_deepseek_api_key(
    *, environ: Mapping[str, str] | None = None, key_file: str | Path = DEFAULT_KEY_FILE,
) -> str:
    """Read a key only from process environment or the ignored private config file."""
    environment = os.environ if environ is None else environ
    value = environment.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        return value
    path = Path(key_file).expanduser().resolve()
    expected_root = DEFAULT_KEY_FILE.parent.resolve()
    try:
        path.relative_to(expected_root)
    except ValueError as exc:
        raise DeepSeekProviderError(
            "api_key_path_invalid", category="configuration", retryable=False,
        ) from exc
    if not path.is_file():
        raise DeepSeekProviderError("api_key_missing", category="configuration", retryable=False)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeepSeekProviderError(
            "api_key_unavailable", category="configuration", retryable=False,
        ) from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        if name.strip() == "DEEPSEEK_API_KEY" and raw.strip():
            return raw.strip().strip('"').strip("'")
    raise DeepSeekProviderError("api_key_missing", category="configuration", retryable=False)


class DeepSeekHTTPAdapter:
    """HTTP-only adapter: it cannot retrieve memory or access any database."""

    provider_id = PROVIDER_ID
    model_id = MODEL_ID

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)

    def send(self, prepared_request: PreparedDeepSeekRequest) -> DeepSeekResponse:
        prepared = _validate_prepared_request(prepared_request)
        key = load_deepseek_api_key()
        request = Request(
            API_URL,
            data=prepared.wire_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            status = int(exc.code)
            raise DeepSeekProviderError(
                "provider_http_error",
                category="provider",
                retryable=status == 429 or status >= 500,
                http_status=status,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise DeepSeekProviderError(
                "provider_network_error", category="transport", retryable=True,
            ) from None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeepSeekProviderError(
                "provider_response_invalid", category="provider", retryable=False,
            ) from None
        return parse_deepseek_response(decoded)


def parse_deepseek_response(payload: Any) -> DeepSeekResponse:
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        usage_raw = payload["usage"]
        usage = {
            "prompt_tokens": usage_raw["prompt_tokens"],
            "completion_tokens": usage_raw["completion_tokens"],
            "total_tokens": usage_raw["total_tokens"],
        }
        response_id = payload["id"]
        model = payload["model"]
        finish_reason = choice["finish_reason"]
    except (KeyError, IndexError, TypeError):
        raise DeepSeekProviderError(
            "provider_response_invalid", category="provider", retryable=False,
        ) from None
    if (
        not isinstance(response_id, str) or not response_id
        or model != MODEL_ID
        or not isinstance(content, str) or not content.strip()
        or not isinstance(finish_reason, str) or not finish_reason
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values())
        or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
    ):
        raise DeepSeekProviderError(
            "provider_response_invalid", category="provider", retryable=False,
        )
    return DeepSeekResponse(
        provider_response_id=response_id,
        model=model,
        content=content.strip(),
        usage=usage,
        finish_reason=finish_reason,
    )


__all__ = [
    "API_URL", "MODEL_ID", "PROVIDER_ID", "DeepSeekHTTPAdapter",
    "DeepSeekProviderError", "DeepSeekResponse", "PreparedDeepSeekRequest",
    "load_deepseek_api_key", "parse_deepseek_response", "prepare_deepseek_request",
    "render_deepseek_wire_body",
]
