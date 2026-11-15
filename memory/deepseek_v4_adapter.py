"""Transport-only DeepSeek v4-flash adapter for YunOS v8.0-D1."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from memory.runtime_protocol_contracts import (
    ARTIFACT_VERSION,
    BASE_URL,
    MODEL_ID,
    PROVIDER_ID,
    RENDERER_VERSION,
    RUNTIME_PROFILE_VERSION,
    PreparedRuntimeRequest,
    RuntimeProtocolContractError,
    prepare_runtime_request,
    render_runtime_wire_body,
)


CHAT_URL = f"{BASE_URL}/chat/completions"
MODELS_URL = f"{BASE_URL}/models"
BALANCE_URL = f"{BASE_URL}/user/balance"
PRODUCTION_PRIVATE_KEY_FILE = (
    Path(__file__).resolve().parents[1] / "config_private" / "deepseek.env"
)


class DeepSeekV4ProviderError(RuntimeError):
    """Sanitized provider/transport failure."""

    def __init__(
        self, code: str, *, category: str, retryable: bool,
        http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.http_status = http_status


def _parse_production_private_credential(raw: str) -> str:
    """Validate the fixed one-assignment private-file format without normalizing it."""
    if not isinstance(raw, str):
        raise DeepSeekV4ProviderError(
            "api_key_format_invalid", category="configuration", retryable=False,
        )
    match = re.fullmatch(r"DEEPSEEK_API_KEY=([^\r\n]+)(?:\r?\n)?", raw)
    value = match.group(1) if match else ""
    if (
        not value
        or value != value.strip()
        or any(char.isspace() or ord(char) < 33 for char in value)
    ):
        raise DeepSeekV4ProviderError(
            "api_key_format_invalid", category="configuration", retryable=False,
        )
    return value


def load_production_deepseek_api_key() -> str:
    """Read exactly the fixed ignored private file; no environment or path fallback."""
    path = PRODUCTION_PRIVATE_KEY_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DeepSeekV4ProviderError(
            "api_key_missing", category="configuration", retryable=False,
        ) from None
    except (OSError, UnicodeError):
        raise DeepSeekV4ProviderError(
            "api_key_unavailable", category="configuration", retryable=False,
        ) from None
    return _parse_production_private_credential(raw)


PreparedDeepSeekV4Request = PreparedRuntimeRequest


@dataclass(frozen=True)
class DeepSeekV4Response:
    provider_response_id: str
    model: str
    content: str
    usage: dict[str, int]
    finish_reason: str


def render_v4_flash_wire_body(payload: dict[str, Any]) -> bytes:
    try:
        return render_runtime_wire_body(payload)
    except RuntimeProtocolContractError as exc:
        raise DeepSeekV4ProviderError(
            exc.reason_code, category="request", retryable=False,
        ) from None


def prepare_v4_flash_request(artifact: Any) -> PreparedDeepSeekV4Request:
    try:
        return prepare_runtime_request(artifact)
    except RuntimeProtocolContractError as exc:
        raise DeepSeekV4ProviderError(
            exc.reason_code, category="request", retryable=False,
        ) from None


def _validate_prepared(value: Any) -> PreparedDeepSeekV4Request:
    if not isinstance(value, PreparedDeepSeekV4Request):
        raise DeepSeekV4ProviderError(
            "request_artifact_invalid", category="request", retryable=False,
        )
    try:
        artifact = json.loads(value.artifact_canonical_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeepSeekV4ProviderError(
            "request_artifact_invalid", category="request", retryable=False,
        ) from None
    if prepare_v4_flash_request(artifact) != value:
        raise DeepSeekV4ProviderError(
            "request_artifact_invalid", category="request", retryable=False,
        )
    return value


def parse_v4_flash_response(payload: Any) -> DeepSeekV4Response:
    try:
        choice = payload["choices"][0]
        usage_raw = payload["usage"]
        usage = {
            "prompt_tokens": usage_raw["prompt_tokens"],
            "completion_tokens": usage_raw["completion_tokens"],
            "total_tokens": usage_raw["total_tokens"],
        }
        response_id = payload["id"]
        model = payload["model"]
        content = choice["message"]["content"]
        finish_reason = choice["finish_reason"]
    except (KeyError, IndexError, TypeError):
        raise DeepSeekV4ProviderError(
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
        raise DeepSeekV4ProviderError(
            "provider_response_invalid", category="provider", retryable=False,
        )
    return DeepSeekV4Response(response_id, model, content, usage, finish_reason)


class DeepSeekV4FlashHTTPAdapter:
    """POST transport; it receives only a fully validated frozen request."""

    provider_id = PROVIDER_ID
    model_id = MODEL_ID
    base_url = BASE_URL

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        if (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)

    def send(self, prepared_request: PreparedDeepSeekV4Request) -> DeepSeekV4Response:
        prepared = _validate_prepared(prepared_request)
        key = load_production_deepseek_api_key()
        request = Request(
            CHAT_URL, data=prepared.wire_body, method="POST",
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
            raise DeepSeekV4ProviderError(
                "provider_http_error", category="provider",
                retryable=status == 429 or status >= 500, http_status=status,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise DeepSeekV4ProviderError(
                "provider_network_error", category="transport", retryable=True,
            ) from None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeepSeekV4ProviderError(
                "provider_response_invalid", category="provider", retryable=False,
            ) from None
        return parse_v4_flash_response(decoded)


READINESS_PROTOCOL_VERSION = "v8.0-d1-deepseek-readiness-2"


def _readiness_get_json(
    url: str,
    credential: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    if url not in (MODELS_URL, BALANCE_URL):
        raise DeepSeekV4ProviderError(
            "readiness_endpoint_not_allowed", category="request", retryable=False,
        )
    request = Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as exc:
        raise DeepSeekV4ProviderError(
            "readiness_http_error", category="provider", retryable=False,
            http_status=int(exc.code),
        ) from None
    except (URLError, TimeoutError, OSError):
        raise DeepSeekV4ProviderError(
            "readiness_network_error", category="transport", retryable=True,
        ) from None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DeepSeekV4ProviderError(
            "readiness_response_invalid", category="provider", retryable=False,
        ) from None
    if not isinstance(decoded, dict):
        raise DeepSeekV4ProviderError(
            "readiness_response_invalid", category="provider", retryable=False,
        )
    return decoded


def _execute_readiness_transport(
    credential: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    if not isinstance(credential, str) or not credential:
        raise DeepSeekV4ProviderError(
            "api_key_missing", category="configuration", retryable=False,
        )
    if (
        isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    models = _readiness_get_json(
        MODELS_URL, credential, timeout_seconds=float(timeout_seconds), opener=opener,
    )
    balance = _readiness_get_json(
        BALANCE_URL, credential, timeout_seconds=float(timeout_seconds), opener=opener,
    )
    model_ids = {
        item.get("id") for item in models.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(models, dict) else set()
    balance_available = balance.get("is_available") if isinstance(balance, dict) else None
    if not isinstance(balance_available, bool):
        raise DeepSeekV4ProviderError(
            "readiness_response_invalid", category="provider", retryable=False,
        )
    return {
        "protocol_version": READINESS_PROTOCOL_VERSION,
        "status": "ready" if MODEL_ID in model_ids and balance_available else "not_ready",
        "checks": {
            "models": {"ok": True, "target_model_available": MODEL_ID in model_ids},
            "balance": {"ok": True, "is_available": balance_available},
        },
        "requests": [
            {"method": "GET", "endpoint": "/models"},
            {"method": "GET", "endpoint": "/user/balance"},
        ],
        "chat_completion_sent": False,
        "credential_persisted": False,
        "response_body_persisted": False,
    }


def _build_production_readiness_transport_boundary():
    """Seal production GETs to exactly one registered runner code object."""

    authorized_codes: set[Any] = set()
    sealed = False

    def authorize(code: Any) -> None:
        nonlocal sealed
        if sealed or not hasattr(code, "co_code"):
            raise DeepSeekV4ProviderError(
                "readiness_transport_authorization_closed",
                category="configuration", retryable=False,
            )
        authorized_codes.add(code)

    def seal() -> None:
        nonlocal sealed
        if len(authorized_codes) != 1:
            raise DeepSeekV4ProviderError(
                "readiness_transport_authorization_invalid",
                category="configuration", retryable=False,
            )
        sealed = True

    def run(credential: str, *, timeout_seconds: float = 20.0) -> dict[str, Any]:
        if not sealed or sys._getframe(1).f_code not in authorized_codes:
            raise DeepSeekV4ProviderError(
                "readiness_transport_authority_required",
                category="authorization", retryable=False,
            )
        return _execute_readiness_transport(
            credential, timeout_seconds=timeout_seconds, opener=urlopen,
        )

    return run, authorize, seal


(
    _run_production_readiness_transport,
    _authorize_production_readiness_runner,
    _seal_production_readiness_transport,
) = _build_production_readiness_transport_boundary()
del _build_production_readiness_transport_boundary


def _run_test_readiness_transport(
    *,
    synthetic_credential: str,
    opener: Callable[..., Any],
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Private test primitive; it never loads a production credential."""

    return _execute_readiness_transport(
        synthetic_credential, timeout_seconds=timeout_seconds, opener=opener,
    )
