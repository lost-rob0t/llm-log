from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Mapping


_USER_MESSAGE_LIMIT = 4000


def _required_string(event: Mapping[str, Any], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"capture event {field} must be a non-empty string")
    return value


def decode_captured_body(body: Any) -> bytes:
    if not isinstance(body, Mapping):
        raise ValueError("captured body must be an object")
    encoding = body.get("encoding")
    if encoding == "utf-8":
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("utf-8 captured body requires text")
        return text.encode("utf-8")
    if encoding == "base64":
        data = body.get("data")
        if not isinstance(data, str):
            raise ValueError("base64 captured body requires data")
        try:
            return base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64 captured body") from exc
    raise ValueError(f"unsupported captured body encoding: {encoding!r}")


def _user_message_from_api_body(body: Mapping[str, Any]) -> str:
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, Mapping) and part.get("type") == "text"
            )
    return ""


def extract_user_message(request_body: bytes) -> str:
    text = request_body.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, Mapping):
        message = _user_message_from_api_body(body)
        if message:
            return message[:_USER_MESSAGE_LIMIT]
    for line in text.splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, Mapping) or frame.get("type") != "text":
            continue
        inner = frame.get("text")
        if not isinstance(inner, str):
            continue
        try:
            parsed = json.loads(inner)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            message = _user_message_from_api_body(parsed)
            if message:
                return message[:_USER_MESSAGE_LIMIT]
    return ""


def validate_capture_event(event: Mapping[str, Any]) -> None:
    _required_string(event, "event_id")
    _required_string(event, "provider")
    _required_string(event, "started_at")
    _required_string(event, "completed_at")
    _required_string(event, "request_sha256")
    _required_string(event, "response_sha256")
    decode_captured_body(event.get("request_body"))


def expert_request_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    validate_capture_event(event)
    model = event.get("model")
    transport = event.get("transport")
    return {
        "provider": event["provider"],
        "model": model if isinstance(model, str) and model else "unknown",
        "transport": transport if isinstance(transport, str) and transport else "http",
        "started_at": event["started_at"],
        "completed_at": event["completed_at"],
        "request_sha256": event["request_sha256"],
        "response_sha256": event["response_sha256"],
    }


def expert_response_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_id = _required_string(event, "event_id")
    payload = expert_request_payload(event)

    response_status = event.get("response_status")
    if not isinstance(response_status, int) or isinstance(response_status, bool):
        raise ValueError("capture event response_status must be an integer")

    latency_ms = event.get("latency_ms")
    if (
        not isinstance(latency_ms, int)
        or isinstance(latency_ms, bool)
        or latency_ms < 0
    ):
        raise ValueError("capture event latency_ms must be a non-negative integer")

    status_kind = event.get("status_kind", "upstream")
    if not isinstance(status_kind, str) or not status_kind:
        raise ValueError("capture event status_kind must be a non-empty string")

    stream_completed = event.get("stream_completed")
    if stream_completed is not None and not isinstance(stream_completed, bool):
        raise ValueError("capture event stream_completed must be boolean or null")
    stream_state = (
        "completed"
        if stream_completed is True
        else "incomplete"
        if stream_completed is False
        else "unknown"
    )

    finish_reason = event.get("finish_reason")
    if finish_reason is not None and (
        not isinstance(finish_reason, str) or not finish_reason
    ):
        raise ValueError("capture event finish_reason must be a non-empty string or null")

    return {
        **payload,
        "request_id": event_id,
        "response_status": response_status,
        "status_kind": status_kind,
        "latency_ms": latency_ms,
        "stream_completed": stream_completed,
        "stream_state": stream_state,
        "finish_reason": finish_reason,
    }


def expert_usage_payload(event: Mapping[str, Any]) -> dict[str, Any] | None:
    event_id = _required_string(event, "event_id")
    token_fields = {
        name: event.get(name)
        for name in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cached_output_tokens",
            "reasoning_tokens",
        )
        if event.get(name) is not None
    }
    if not token_fields:
        return None
    provider = _required_string(event, "provider")
    model = event.get("model")
    transport = event.get("transport")
    return {
        "usage_id": f"capture-usage:{event_id}",
        "request_id": event_id,
        "provider": provider,
        "model": model if isinstance(model, str) and model else "unknown",
        "client": "proxy",
        "transport": transport if isinstance(transport, str) and transport else "http",
        **token_fields,
    }


def transport_outcome_payload(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    event_id = _required_string(event, "event_id")
    observed_at = _required_string(event, "completed_at")
    status = event.get("response_status")
    if not isinstance(status, int) or isinstance(status, bool):
        raise ValueError("capture event response_status must be an integer")
    outcome_event_id = f"capture-transport:{event_id}"
    return outcome_event_id, {
        "scope": "request",
        "scope_id": event_id,
        "evidence": [
            {
                "evidence_id": outcome_event_id,
                "observed_at": observed_at,
                "evidence_type": "provider_transport",
                "authority": "weak",
                "observed_value": status,
                "source_id": event_id,
            }
        ],
    }


async def replay_capture_event(
    expert_plane: Any,
    event: Mapping[str, Any],
    *,
    record_transport_evidence: bool = False,
) -> dict[str, Any]:
    payload = expert_request_payload(event)
    event_id = _required_string(event, "event_id")
    request_body = decode_captured_body(event.get("request_body"))

    observed = await expert_plane.observe_request(
        event_id=event_id,
        payload=payload,
        session_id="backfill",
        task_id="backfill",
    )

    classified = None
    message = extract_user_message(request_body)
    if message:
        classified = await expert_plane.classify_request(
            event_id=event_id,
            payload={
                **payload,
                "message": message,
                "user_message_id": "um-" + event_id[:8],
                "request_id": event_id,
                "client": "proxy",
            },
            session_id="backfill",
            task_id="backfill",
        )

    response = await expert_plane.observe_response(
        event_id=event_id,
        payload=expert_response_payload(event),
        session_id="backfill",
        task_id="backfill",
    )

    usage = None
    usage_payload = expert_usage_payload(event)
    if usage_payload is not None:
        usage = await expert_plane.observe_usage(
            event_id=event_id,
            payload=usage_payload,
            session_id="backfill",
            task_id="backfill",
        )

    outcome = None
    if record_transport_evidence:
        outcome_event_id, outcome_payload = transport_outcome_payload(event)
        outcome = await expert_plane.record_outcome_evidence(
            event_id=outcome_event_id,
            payload=outcome_payload,
            session_id="backfill",
            task_id="backfill",
        )

    return {
        "event_id": event_id,
        "request": observed,
        "classification": classified,
        "response": response,
        "usage": usage,
        "transport_outcome": outcome,
    }
