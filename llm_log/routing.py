from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

_MAX_ATTEMPTS = 32
_MAX_LABEL_LENGTH = 160
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class RouterAttempt:
    provider: str | None = None
    status: int | None = None
    error_code: int | str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class RoutingObservation:
    routing_id: str
    event_id: str
    observed_at: str
    router: str
    selected_provider: str | None
    attempts: tuple[RouterAttempt, ...]
    source: str = "provider_metadata"

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "routing_id": self.routing_id,
            "event_id": self.event_id,
            "observed_at": self.observed_at,
            "router": self.router,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.as_json() for attempt in self.attempts],
            "source": self.source,
        }
        if self.selected_provider is not None:
            payload["selected_provider"] = self.selected_provider
        return payload


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > _MAX_LABEL_LENGTH:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _safe_status(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _safe_code(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
        return value
    return None


def _documents(raw: bytes) -> list[Mapping[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []

    documents: list[Mapping[str, Any]] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        documents.append(parsed)

    for line in text.splitlines():
        payload = line.strip()
        if payload.startswith("data:"):
            payload = payload[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping) and parsed.get("type") == "text":
            inner = parsed.get("text")
            if isinstance(inner, str):
                try:
                    parsed = json.loads(inner)
                except json.JSONDecodeError:
                    continue
        if isinstance(parsed, Mapping):
            documents.append(parsed)
    return documents


def _attempt_from_mapping(value: Mapping[str, Any]) -> RouterAttempt | None:
    provider = _safe_label(value.get("provider")) or _safe_label(value.get("provider_name"))
    status = _safe_status(value.get("status")) or _safe_status(value.get("status_code"))

    error_code = _safe_code(value.get("error_code"))
    error = value.get("error")
    if error_code is None and isinstance(error, Mapping):
        error_code = _safe_code(error.get("code"))

    if provider is None and status is None and error_code is None:
        return None
    return RouterAttempt(provider=provider, status=status, error_code=error_code)


def _openrouter_metadata(document: Mapping[str, Any]) -> Mapping[str, Any] | None:
    metadata = document.get("openrouter_metadata")
    return metadata if isinstance(metadata, Mapping) else None


def _metadata_selected_provider(document: Mapping[str, Any]) -> str | None:
    metadata = _openrouter_metadata(document)
    if metadata is None:
        return None
    endpoints = metadata.get("endpoints")
    if not isinstance(endpoints, Mapping):
        return None
    available = endpoints.get("available")
    if not isinstance(available, list):
        return None
    for endpoint in available:
        if not isinstance(endpoint, Mapping) or endpoint.get("selected") is not True:
            continue
        provider = _safe_label(endpoint.get("provider")) or _safe_label(endpoint.get("provider_name"))
        if provider is not None:
            return provider
    return None


def _metadata_attempts(document: Mapping[str, Any]) -> list[RouterAttempt]:
    metadata = _openrouter_metadata(document)
    if metadata is None:
        return []
    raw_attempts = metadata.get("attempts")
    if not isinstance(raw_attempts, list):
        return []

    attempts: list[RouterAttempt] = []
    for raw_attempt in raw_attempts[:_MAX_ATTEMPTS]:
        if not isinstance(raw_attempt, Mapping):
            continue
        attempt = _attempt_from_mapping(raw_attempt)
        if attempt is not None:
            attempts.append(attempt)
    return attempts


def _error_provider_attempt(document: Mapping[str, Any]) -> RouterAttempt | None:
    error = document.get("error")
    if not isinstance(error, Mapping):
        return None
    metadata = error.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    provider = _safe_label(metadata.get("provider_name")) or _safe_label(metadata.get("provider"))
    if provider is None:
        return None
    return RouterAttempt(provider=provider, error_code=_safe_code(error.get("code")))


def observe_openrouter_routing(
    *,
    event_id: str,
    observed_at: str,
    provider: str,
    response_body: bytes,
) -> RoutingObservation | None:
    if provider.casefold() != "openrouter":
        return None

    selected_provider: str | None = None
    attempts: list[RouterAttempt] = []
    seen_attempts: set[tuple[str | None, int | None, int | str | None]] = set()

    for document in _documents(response_body):
        disclosed_provider = (
            _safe_label(document.get("provider"))
            or _metadata_selected_provider(document)
        )
        if disclosed_provider is not None:
            selected_provider = disclosed_provider

        candidates = _metadata_attempts(document)
        if not candidates:
            fallback = _error_provider_attempt(document)
            if fallback is not None:
                candidates = [fallback]

        for attempt in candidates:
            identity = (attempt.provider, attempt.status, attempt.error_code)
            if identity in seen_attempts:
                continue
            seen_attempts.add(identity)
            attempts.append(attempt)
            if len(attempts) >= _MAX_ATTEMPTS:
                break
        if len(attempts) >= _MAX_ATTEMPTS:
            break

    if selected_provider is None and not attempts:
        return None

    return RoutingObservation(
        routing_id=f"routing:{event_id}",
        event_id=event_id,
        observed_at=observed_at,
        router="openrouter",
        selected_provider=selected_provider,
        attempts=tuple(attempts),
    )
