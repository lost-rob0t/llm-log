from __future__ import annotations

import base64
import binascii
import hashlib
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


def _extract_full_user_message(request_body: bytes) -> str:
    text = request_body.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, Mapping):
        message = _user_message_from_api_body(body)
        if message:
            return message
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
                return message
    return ""


def extract_user_message_record(request_body: bytes) -> tuple[str, bool, str]:
    message = _extract_full_user_message(request_body)
    if not message:
        return "", False, hashlib.sha256(b"").hexdigest()
    encoded = message.encode("utf-8")
    return (
        message[:_USER_MESSAGE_LIMIT],
        len(message) > _USER_MESSAGE_LIMIT,
        hashlib.sha256(encoded).hexdigest(),
    )


def extract_user_message(request_body: bytes) -> str:
    message, _truncated, _sha256 = extract_user_message_record(request_body)
    return message


def validate_capture_event(event: Mapping[str, Any]) -> None:
    _required_string(event, "event_id")
    _required_string(event, "provider")
    _required_string(event, "started_at")
    _required_string(event, "completed_at")
    _required_string(event, "request_sha256")
    _required_string(event, "response_sha256")
    decode_captured_body(event.get("request_body"))


def _attribution(event: Mapping[str, Any]) -> dict[str, str]:
    value = event.get("attribution")
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str) and item
    }


def _optional_string(event: Mapping[str, Any], field: str) -> str | None:
    value = event.get(field)
    return value if isinstance(value, str) and value else None


def expert_request_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    validate_capture_event(event)
    model = event.get("model")
    transport = event.get("transport")
    payload: dict[str, Any] = {
        "provider": event["provider"],
        "upstream": _optional_string(event, "upstream"),
        "model": model if isinstance(model, str) and model else "unknown",
        "transport": transport if isinstance(transport, str) and transport else "http",
        "method": _optional_string(event, "method"),
        "path": _optional_string(event, "path"),
        "query": event.get("query") if isinstance(event.get("query"), str) else "",
        "started_at": event["started_at"],
        "completed_at": event["completed_at"],
        "latency_ms": event.get("latency_ms"),
        "request_sha256": event["request_sha256"],
        "response_sha256": event["response_sha256"],
        "attribution": _attribution(event),
    }
    return payload


def expert_user_message_payload(
    event: Mapping[str, Any], request_body: bytes
) -> dict[str, Any] | None:
    event_id = _required_string(event, "event_id")
    message, truncated, message_sha256 = extract_user_message_record(request_body)
    if not message:
        return None
    attribution = _attribution(event)
    model = event.get("model")
    return {
        "message_id": f"capture-user-message:{event_id}",
        "request_id": event_id,
        "message": message,
        "message_truncated": truncated,
        "message_sha256": message_sha256,
        "provider": event["provider"],
        "model": model if isinstance(model, str) and model else "unknown",
        "client": attribution.get("agent")
        or attribution.get("worker")
        or "proxy",
        "attribution": attribution,
    }


def _response_json_documents(raw: bytes) -> list[Any]:
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
        item = line.strip()
        if item.startswith("data:"):
            item = item[5:].strip()
        if not item or item == "[DONE]":
            continue
        try:
            documents.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    return documents


def _find_finish_reason(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("finish_reason", "stop_reason"):
            reason = value.get(key)
            if isinstance(reason, str) and reason:
                return reason
        for child in value.values():
            reason = _find_finish_reason(child)
            if reason is not None:
                return reason
    elif isinstance(value, list):
        for child in value:
            reason = _find_finish_reason(child)
            if reason is not None:
                return reason
    return None


def expert_response_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    event_id = _required_string(event, "event_id")
    status = event.get("response_status")
    if not isinstance(status, int) or isinstance(status, bool):
        raise ValueError("capture event response_status must be an integer")
    response_body = decode_captured_body(event.get("response_body"))
    reasons = [
        reason
        for document in _response_json_documents(response_body)
        if (reason := _find_finish_reason(document)) is not None
    ]
    model = event.get("model")
    transport = event.get("transport")
    return {
        "response_id": f"capture-response:{event_id}",
        "request_id": event_id,
        "provider": event["provider"],
        "model": model if isinstance(model, str) and model else "unknown",
        "transport": transport if isinstance(transport, str) and transport else "http",
        "response_status": status,
        "completed_at": event["completed_at"],
        "latency_ms": event.get("latency_ms"),
        "response_sha256": event["response_sha256"],
        "finish_reason": reasons[-1] if reasons else None,
        "attribution": _attribution(event),
    }


def response_assessment_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    response = expert_response_payload(event)
    return {
        "request_id": response["request_id"],
        "response_id": response["response_id"],
        "response_status": response["response_status"],
        "latency_ms": response["latency_ms"],
        "transport": response["transport"],
        "finish_reason": response["finish_reason"],
        "usage_observed": expert_usage_payload(event) is not None,
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
    default_session_id: str = "backfill",
    default_task_id: str = "backfill",
) -> dict[str, Any]:
    payload = expert_request_payload(event)
    event_id = _required_string(event, "event_id")
    request_body = decode_captured_body(event.get("request_body"))
    attribution = _attribution(event)
    session_id = attribution.get("session") or default_session_id
    task_id = attribution.get("task") or default_task_id

    observed = await expert_plane.observe_request(
        event_id=event_id,
        payload=payload,
        session_id=session_id,
        task_id=task_id,
    )

    user_message = None
    classified = None
    message_payload = expert_user_message_payload(event, request_body)
    if message_payload is not None:
        user_message = await expert_plane.observe_user_message(
            event_id=message_payload["message_id"],
            payload=message_payload,
            session_id=session_id,
            task_id=task_id,
        )
        classified = await expert_plane.classify_request(
            event_id=event_id,
            payload={
                **payload,
                "message": message_payload["message"],
                "message_truncated": message_payload["message_truncated"],
                "message_sha256": message_payload["message_sha256"],
                "user_message_id": message_payload["message_id"],
                "request_id": event_id,
                "client": message_payload["client"],
                "session_id": session_id,
                "task_id": task_id,
            },
            session_id=session_id,
            task_id=task_id,
        )

    response_payload = expert_response_payload(event)
    response = await expert_plane.observe_response(
        event_id=response_payload["response_id"],
        payload=response_payload,
        session_id=session_id,
        task_id=task_id,
    )
    assessment = await expert_plane.assess_response(
        event_id=event_id,
        payload=response_assessment_payload(event),
        session_id=session_id,
        task_id=task_id,
    )

    usage = None
    usage_payload = expert_usage_payload(event)
    if usage_payload is not None:
        usage = await expert_plane.observe_usage(
            event_id=event_id,
            payload=usage_payload,
            session_id=session_id,
            task_id=task_id,
        )

    outcome = None
    if record_transport_evidence:
        outcome_event_id, outcome_payload = transport_outcome_payload(event)
        outcome = await expert_plane.record_outcome_evidence(
            event_id=outcome_event_id,
            payload=outcome_payload,
            session_id=session_id,
            task_id=task_id,
        )

    return {
        "event_id": event_id,
        "request": observed,
        "user_message": user_message,
        "classification": classified,
        "response": response,
        "response_assessment": assessment,
        "usage": usage,
        "transport_outcome": outcome,
    }
