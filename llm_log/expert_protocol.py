from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

PROTOCOL_VERSION = 1

_ALLOWED_OPERATIONS = frozenset(
    {
        "observe_user_message",
        "observe_request",
        "observe_response",
        "observe_usage",
        "query_classification",
        "query_task",
        "query_outcome",
        "query_rewrite",
        "health",
    }
)
_OBSERVATION_OPERATIONS = frozenset(
    {"observe_user_message", "observe_request", "observe_response", "observe_usage"}
)


class ExpertProtocolError(ValueError):
    pass


class ExpertMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    APPLY = "apply"


@dataclass(frozen=True, slots=True)
class ExpertEnvelope:
    version: int
    operation: str
    event_id: str | None
    session_id: str | None
    task_id: str | None
    payload: Mapping[str, Any]


def _validate_envelope(envelope: ExpertEnvelope) -> None:
    if envelope.version != PROTOCOL_VERSION:
        raise ExpertProtocolError(f"unsupported expert protocol version: {envelope.version}")
    if envelope.operation not in _ALLOWED_OPERATIONS:
        raise ExpertProtocolError(f"unknown expert operation: {envelope.operation}")
    if envelope.operation in _OBSERVATION_OPERATIONS and not envelope.event_id:
        raise ExpertProtocolError(f"{envelope.operation} requires a stable event_id")
    if not isinstance(envelope.payload, Mapping):
        raise ExpertProtocolError("payload must be an object")


def encode_envelope(envelope: ExpertEnvelope) -> bytes:
    _validate_envelope(envelope)
    payload = {
        "version": envelope.version,
        "operation": envelope.operation,
        "event_id": envelope.event_id,
        "session_id": envelope.session_id,
        "task_id": envelope.task_id,
        "payload": dict(envelope.payload),
    }
    try:
        return (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExpertProtocolError(f"payload is not JSON serializable: {exc}") from exc


def decode_envelope(data: bytes | str) -> ExpertEnvelope:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ExpertProtocolError(f"invalid expert protocol JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ExpertProtocolError("expert envelope must be a JSON object")

    try:
        envelope = ExpertEnvelope(
            version=raw["version"],
            operation=raw["operation"],
            event_id=raw.get("event_id"),
            session_id=raw.get("session_id"),
            task_id=raw.get("task_id"),
            payload=raw.get("payload", {}),
        )
    except KeyError as exc:
        raise ExpertProtocolError(
            f"missing required envelope field: {exc.args[0]}"
        ) from exc

    if not isinstance(envelope.version, int) or isinstance(envelope.version, bool):
        raise ExpertProtocolError("version must be an integer")
    if not isinstance(envelope.operation, str):
        raise ExpertProtocolError("operation must be a string")
    for name, value in (
        ("event_id", envelope.event_id),
        ("session_id", envelope.session_id),
        ("task_id", envelope.task_id),
    ):
        if value is not None and not isinstance(value, str):
            raise ExpertProtocolError(f"{name} must be a string or null")

    _validate_envelope(envelope)
    return envelope
