from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

_RESET_ERRNOS = {54, 104}
_ACCOUNT_OR_GATEWAY_STATUSES = {401, 403, 429}
_MAX_ERROR_CODE_LENGTH = 128


@dataclass(frozen=True, slots=True)
class TransportErrorEvidence:
    error_id: str
    event_id: str
    observed_at: str
    error_class: str
    domain: str
    severity: str
    attribution_scope: str
    provider: str | None
    model: str | None
    transport: str
    response_status: int | None
    status_kind: str | None
    error_code: int | str | None
    request_sha256: str | None
    source: str = "proxy"

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _documents(raw: bytes) -> list[Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []

    documents: list[Any] = []
    try:
        documents.append(json.loads(text))
    except json.JSONDecodeError:
        pass

    for line in text.splitlines():
        payload = line.strip()
        if payload.startswith("data:"):
            payload = payload[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            documents.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return documents


def _find_error(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        error = value.get("error")
        if isinstance(error, Mapping):
            return error
        for child in value.values():
            found = _find_error(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_error(child)
            if found is not None:
                return found
    return None


def _safe_error_code(error: Mapping[str, Any] | None) -> int | str | None:
    if error is None:
        return None
    value = error.get("code")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return value[:_MAX_ERROR_CODE_LENGTH]
    return None


def _has_router_metadata(error: Mapping[str, Any] | None) -> bool:
    return error is not None and isinstance(error.get("metadata"), Mapping)


def _reset_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, ConnectionResetError):
        return True
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int) and errno in _RESET_ERRNOS:
        return True
    return type(exc).__name__ in {
        "ClientConnectionResetError",
        "ServerDisconnectedError",
    }


def _exception_code(exc: BaseException | None) -> int | str | None:
    if exc is None:
        return None
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        return errno
    name = type(exc).__name__
    return name[:_MAX_ERROR_CODE_LENGTH] if name else None


def _severity(error_class: str) -> str:
    return "info" if error_class == "downstream_client_disconnect" else "warning"


def _evidence(
    *,
    event_id: str,
    observed_at: str,
    error_class: str,
    attribution_scope: str,
    provider: str | None,
    model: str | None,
    transport: str,
    response_status: int | None,
    status_kind: str | None,
    error_code: int | str | None,
    request_sha256: str | None,
) -> TransportErrorEvidence:
    return TransportErrorEvidence(
        error_id=f"transport:{event_id}:{error_class}",
        event_id=event_id,
        observed_at=observed_at,
        error_class=error_class,
        domain="transport",
        severity=_severity(error_class),
        attribution_scope=attribution_scope,
        provider=provider,
        model=model,
        transport=transport,
        response_status=response_status,
        status_kind=status_kind,
        error_code=error_code,
        request_sha256=request_sha256,
    )


def classify_downstream_disconnect(
    *,
    event_id: str,
    observed_at: str,
    provider: str | None,
    model: str | None,
    transport: str,
    response_status: int | None,
    status_kind: str | None,
    request_sha256: str | None,
    exc: BaseException,
) -> TransportErrorEvidence:
    return _evidence(
        event_id=event_id,
        observed_at=observed_at,
        error_class="downstream_client_disconnect",
        attribution_scope="client",
        provider=provider,
        model=model,
        transport=transport,
        response_status=response_status,
        status_kind=status_kind,
        error_code=_exception_code(exc),
        request_sha256=request_sha256,
    )


def classify_completed_capture(
    *,
    event_id: str,
    observed_at: str,
    provider: str | None,
    model: str | None,
    transport: str,
    response_status: int,
    status_kind: str,
    stream_completed: bool | None,
    finish_reason: str | None,
    request_sha256: str | None,
    response_body: bytes,
    terminal_exception: BaseException | None = None,
) -> TransportErrorEvidence | None:
    documents = _documents(response_body)
    error = next(
        (found for document in documents if (found := _find_error(document)) is not None),
        None,
    )
    error_code = _safe_error_code(error)

    if _reset_error(terminal_exception):
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="upstream_connection_reset",
            attribution_scope="provider_or_network",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=_exception_code(terminal_exception),
            request_sha256=request_sha256,
        )

    if response_status in _ACCOUNT_OR_GATEWAY_STATUSES:
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="upstream_http_error",
            attribution_scope="gateway_or_account",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=error_code,
            request_sha256=request_sha256,
        )

    if error is not None and (
        _has_router_metadata(error)
        or (provider is not None and provider.casefold() == "openrouter")
    ):
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="router_error",
            attribution_scope="gateway",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=error_code,
            request_sha256=request_sha256,
        )

    if response_status >= 400:
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="upstream_http_error",
            attribution_scope="provider_or_gateway",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=error_code,
            request_sha256=request_sha256,
        )

    if error is not None or finish_reason == "error":
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="upstream_stream_error",
            attribution_scope="provider_or_gateway",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=error_code,
            request_sha256=request_sha256,
        )

    if stream_completed is False:
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="stream_protocol_error",
            attribution_scope="provider_or_gateway",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=None,
            request_sha256=request_sha256,
        )

    if terminal_exception is not None:
        return _evidence(
            event_id=event_id,
            observed_at=observed_at,
            error_class="unknown_transport_failure",
            attribution_scope="transport",
            provider=provider,
            model=model,
            transport=transport,
            response_status=response_status,
            status_kind=status_kind,
            error_code=_exception_code(terminal_exception),
            request_sha256=request_sha256,
        )

    return None
