"""Extract explicit failure evidence from caller-supplied normalized captures.

This module never reads agent home directories and never decides outcomes. The
existing expert ingestion operation persists evidence and Prolog owns decisions.
Only response-side structured fields are inspected; prose and absent fields are
not evidence of failure. Source IDs identify the capture and exact JSON location.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from .expert_capture import decode_captured_body

MINER_VERSION = "session-failures/v1"
_ERROR_CODES = {
    "insufficient_quota": "quota_exceeded",
    "quota_exceeded": "quota_exceeded",
    "authentication_error": "authentication_error",
    "invalid_api_key": "authentication_error",
    "permission_denied": "permission_denied",
    "permission_error": "permission_denied",
    "rate_limit_exceeded": "rate_limit_exceeded",
    "rate_limit_error": "rate_limit_exceeded",
}
_FINISH_STATES = {
    "length": "truncated", "max_tokens": "truncated",
    "max_output_tokens": "truncated", "truncated": "truncated",
    "aborted": "aborted", "cancelled": "cancelled", "canceled": "cancelled",
    "timeout": "timeout", "timed_out": "timeout",
}


def _required(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"capture event {key} must be a non-empty string")
    return value


def _response_documents(event: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    body = event.get("response_body")
    if body is None:
        return []
    text = decode_captured_body(body).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Captured SSE and websocket envelopes contain explicit JSON records.
        result = []
        for index, line in enumerate(text.splitlines()):
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                if parsed.get("type") == "text" and isinstance(parsed.get("text"), str):
                    try:
                        inner = json.loads(parsed["text"])
                    except json.JSONDecodeError:
                        inner = None
                    if isinstance(inner, Mapping):
                        parsed = inner
                result.append((f"response_body/line/{index}", parsed))
        return result
    if isinstance(parsed, Mapping):
        path = "response_body"
        if parsed.get("type") == "text" and isinstance(parsed.get("text"), str):
            try:
                inner = json.loads(parsed["text"])
            except json.JSONDecodeError:
                inner = None
            if isinstance(inner, Mapping):
                parsed, path = inner, f"{path}/text"
        return [(path, parsed)]
    return []


def mine_capture_failures(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a deterministic record_outcome_evidence envelope, or no evidence.

    The caller must ingest the capture using the existing request/usage contract
    before replaying this envelope. A nonzero exit code or explicit tool error
    describes this request only, never an overall session/task outcome.
    """
    event_id = _required(event, "event_id")
    observed_at = _required(event, "completed_at")
    observations: set[tuple[str, str, str | int, str]] = set()

    def add(kind: str, value: str | int, path: str, authority: str = "normal") -> None:
        observations.add((kind, authority, value, f"{event_id}#{path}"))

    status = event.get("response_status")
    if isinstance(status, int) and not isinstance(status, bool) and (
        status in (401, 403, 429) or 500 <= status <= 599
    ):
        add("provider_transport", status, "response_status", "weak")
    attempts = event.get("attempts")
    if isinstance(attempts, list) and attempts and isinstance(attempts[-1], Mapping):
        terminal = attempts[-1].get("terminal_class")
        if terminal == "midstream_failure":
            add("task_state", "aborted", f"attempts/{len(attempts)-1}/terminal_class")
        elif terminal == "transport_failure":
            add("provider_transport", "error", f"attempts/{len(attempts)-1}/terminal_class")

    def inspect(node: Mapping[str, Any], path: str, *, tool: bool = False) -> None:
        if tool:
            state = node.get("state")
            state = state if isinstance(state, Mapping) else node
            failed = state.get("status") in ("error", "failed", "failure")
            failed = failed or state.get("is_error") is True
            tool_error = state.get("error")
            failed = failed or (isinstance(tool_error, (str, Mapping)) and bool(tool_error))
            exit_code = state.get("exit_code")
            failed = failed or (
                isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0
            )
            if failed:
                add("tool_result", "failure", path)
            return
        error = node.get("error")
        if isinstance(error, Mapping) and error:
            code = error.get("code") or error.get("type")
            add("provider_transport", _ERROR_CODES.get(str(code), "error"), f"{path}/error")
        for key in ("finish", "finish_reason", "stop_reason", "status"):
            state = node.get(key)
            if isinstance(state, str) and state in _FINISH_STATES:
                add("task_state", _FINISH_STATES[state], f"{path}/{key}")
        # Responses API distinguishes incomplete generation from successful completion.
        details = node.get("incomplete_details")
        if node.get("status") == "incomplete" and isinstance(details, Mapping):
            reason = details.get("reason")
            if reason in ("max_output_tokens", "max_tokens"):
                add("task_state", "truncated", f"{path}/incomplete_details/reason")
        for key in ("choices", "content", "output"):
            children = node.get(key)
            if not isinstance(children, list):
                continue
            for index, child in enumerate(children):
                if isinstance(child, Mapping):
                    child_tool = child.get("type") in ("tool", "tool_result", "function_call_output")
                    inspect(child, f"{path}/{key}/{index}", tool=child_tool)
        for key in ("response", "delta", "message"):
            child = node.get(key)
            if isinstance(child, Mapping):
                inspect(child, f"{path}/{key}")

    for path, document in _response_documents(event):
        inspect(document, path)
    if not observations:
        return None
    if len(observations) > 64:
        raise ValueError("mined failure evidence exceeds expert limit of 64 items")
    evidence = []
    for kind, authority, value, source in sorted(observations, key=lambda item: repr(item)):
        identity = json.dumps([MINER_VERSION, source, kind, authority, value], separators=(",", ":"))
        evidence.append({
            "evidence_id": "mined:" + hashlib.sha256(identity.encode()).hexdigest(),
            "observed_at": observed_at, "evidence_type": kind,
            "authority": authority, "observed_value": value, "source_id": source,
        })
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    history = event.get("history")
    session_id = history.get("session_sha256") if isinstance(history, Mapping) else None
    return {
        "event_id": f"mined-failures:{event_id}:{digest}",
        "session_id": session_id or "backfill", "task_id": "backfill",
        "payload": {"scope": "request", "scope_id": event_id, "evidence": evidence},
    }


def mine_history_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Mine explicitly supplied normalized history/capture records in input order."""
    return [envelope for event in events if (envelope := mine_capture_failures(event)) is not None]


async def replay_mined_failures(expert_plane: Any, event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Persist extracted evidence through the existing expert owner, without retries."""
    envelope = mine_capture_failures(event)
    if envelope is None:
        return None
    return await expert_plane.record_outcome_evidence(**envelope)
