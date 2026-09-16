from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .schema_validation import validate_instance

_DETECTOR_VERSION = "tool-call-integrity/1"
_SCHEMA_DETECTOR_VERSION = "tool-call-schema/1"
_MAX_TOOL_CALLS = 128
_MAX_REQUESTED_TOOLS = 128
_MAX_TOOL_NAME_LENGTH = 256
_MAX_QUANTIZATION_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ReconstructedToolCall:
    index: int
    tool_call_id: str | None
    name: str | None
    arguments_text: str


@dataclass(frozen=True, slots=True)
class ToolCallAnomaly:
    anomaly_id: str
    event_id: str
    provider: str
    model: str
    detector_id: str
    detector_version: str
    domain: str
    score: float
    severity: str
    quantization: str
    evidence: dict[str, Any]
    source: str = "response_tool_call"

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _safe_string(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or len(value) > limit:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _response_documents(raw: bytes) -> list[Mapping[str, Any]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return [parsed]

    documents: list[Mapping[str, Any]] = []
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
        if isinstance(parsed, Mapping):
            documents.append(parsed)
    return documents


def _tool_call_fragments(document: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    fragments: list[tuple[int, Mapping[str, Any]]] = []
    choices = document.get("choices")
    if not isinstance(choices, list):
        return fragments

    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        container: Mapping[str, Any] | None = None
        delta = choice.get("delta")
        message = choice.get("message")
        if isinstance(delta, Mapping):
            container = delta
        elif isinstance(message, Mapping):
            container = message
        if container is None:
            continue

        raw_calls = container.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for ordinal, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                continue
            raw_index = raw_call.get("index")
            index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else ordinal
            if index < 0 or index >= _MAX_TOOL_CALLS:
                continue
            fragments.append((index, raw_call))
    return fragments


def _merge_tool_name(current: str, fragment: str) -> str:
    """Merge provider name fragments without duplicating cumulative repeats."""
    if not current:
        return fragment[:_MAX_TOOL_NAME_LENGTH]
    if fragment == current or current.endswith(fragment):
        return current
    if fragment.startswith(current):
        return fragment[:_MAX_TOOL_NAME_LENGTH]
    if current.startswith(fragment):
        return current
    if len(current) + len(fragment) > _MAX_TOOL_NAME_LENGTH:
        return current
    return current + fragment


def reconstruct_tool_calls(response_body: bytes) -> list[ReconstructedToolCall]:
    accumulators: dict[int, dict[str, Any]] = {}

    for document in _response_documents(response_body):
        for index, fragment in _tool_call_fragments(document):
            state = accumulators.setdefault(
                index,
                {"tool_call_id": None, "name": "", "argument_parts": []},
            )

            tool_call_id = _safe_string(fragment.get("id"), limit=512)
            if state["tool_call_id"] is None and tool_call_id is not None:
                state["tool_call_id"] = tool_call_id

            function = fragment.get("function")
            if not isinstance(function, Mapping):
                continue

            name = function.get("name")
            if isinstance(name, str) and name:
                state["name"] = _merge_tool_name(state["name"], name)

            arguments = function.get("arguments")
            if isinstance(arguments, str):
                state["argument_parts"].append(arguments)

    calls: list[ReconstructedToolCall] = []
    for index in sorted(accumulators):
        state = accumulators[index]
        name_text = state["name"]
        calls.append(
            ReconstructedToolCall(
                index=index,
                tool_call_id=state["tool_call_id"],
                name=name_text or None,
                arguments_text="".join(state["argument_parts"]),
            )
        )
    return calls


def _request_json(request_body: bytes) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(request_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _requested_tools(
    request_body: bytes,
) -> tuple[set[str] | None, dict[str, Mapping[str, Any]]]:
    request = _request_json(request_body)
    if request is None:
        return None, {}

    names: set[str] = set()
    schemas: dict[str, Mapping[str, Any]] = {}

    tools = request.get("tools")
    if isinstance(tools, list):
        for tool in tools[:_MAX_REQUESTED_TOOLS]:
            if not isinstance(tool, Mapping):
                continue
            function = tool.get("function")
            if not isinstance(function, Mapping):
                continue
            name = _safe_string(function.get("name"), limit=_MAX_TOOL_NAME_LENGTH)
            if name is None:
                continue
            names.add(name)
            parameters = function.get("parameters")
            if isinstance(parameters, Mapping):
                schemas.setdefault(name, parameters)

    functions = request.get("functions")
    if isinstance(functions, list):
        for function in functions[:_MAX_REQUESTED_TOOLS]:
            if not isinstance(function, Mapping):
                continue
            name = _safe_string(function.get("name"), limit=_MAX_TOOL_NAME_LENGTH)
            if name is None:
                continue
            names.add(name)
            parameters = function.get("parameters")
            if isinstance(parameters, Mapping):
                schemas.setdefault(name, parameters)

    return names, schemas


def requested_tool_names(request_body: bytes) -> set[str] | None:
    names, _ = _requested_tools(request_body)
    return names


def _quantization(value: str | None) -> str:
    if value is None:
        return "unknown"
    safe = _safe_string(value, limit=_MAX_QUANTIZATION_LENGTH)
    return safe if safe is not None else "unknown"


def _anomaly(
    *,
    event_id: str,
    provider: str,
    model: str,
    detector_id: str,
    score: float,
    severity: str,
    quantization: str,
    call: ReconstructedToolCall,
    detector_version: str = _DETECTOR_VERSION,
    extra_evidence: Mapping[str, Any] | None = None,
) -> ToolCallAnomaly:
    evidence: dict[str, Any] = {
        "tool_call_index": call.index,
        "arguments_length": len(call.arguments_text),
    }
    if call.name is not None:
        evidence["tool_name"] = call.name
    if extra_evidence:
        evidence.update(extra_evidence)

    return ToolCallAnomaly(
        anomaly_id=f"anomaly:{event_id}:{detector_id}:{call.index}",
        event_id=event_id,
        provider=provider,
        model=model,
        detector_id=detector_id,
        detector_version=detector_version,
        domain="model_behavior",
        score=score,
        severity=severity,
        quantization=quantization,
        evidence=evidence,
    )


def analyze_tool_calls(
    *,
    event_id: str,
    provider: str,
    model: str,
    request_body: bytes,
    response_body: bytes,
    observed_quantization: str | None = None,
) -> list[ToolCallAnomaly]:
    calls = reconstruct_tool_calls(response_body)
    if not calls:
        return []

    requested, schemas = _requested_tools(request_body)
    quantization = _quantization(observed_quantization)
    anomalies: list[ToolCallAnomaly] = []

    for call in calls:
        parsed_arguments: Any = None
        arguments_valid_json = True
        try:
            parsed_arguments = json.loads(call.arguments_text)
        except json.JSONDecodeError:
            arguments_valid_json = False
            anomalies.append(
                _anomaly(
                    event_id=event_id,
                    provider=provider,
                    model=model,
                    detector_id="tool_arguments_invalid_json",
                    score=1.0,
                    severity="high",
                    quantization=quantization,
                    call=call,
                    extra_evidence={"validation": "json_decode_failed"},
                )
            )

        tool_requested = requested is not None and call.name is not None and call.name in requested
        if requested is not None and not tool_requested:
            anomalies.append(
                _anomaly(
                    event_id=event_id,
                    provider=provider,
                    model=model,
                    detector_id="tool_name_not_requested",
                    score=0.9,
                    severity="high",
                    quantization=quantization,
                    call=call,
                    extra_evidence={"validation": "tool_name_not_declared"},
                )
            )

        if not arguments_valid_json or not tool_requested or call.name is None:
            continue
        schema = schemas.get(call.name)
        if schema is None:
            continue

        validation = validate_instance(schema, parsed_arguments)
        if validation.state != "violation" or validation.violation is None:
            continue
        anomalies.append(
            _anomaly(
                event_id=event_id,
                provider=provider,
                model=model,
                detector_id="tool_arguments_schema_violation",
                detector_version=_SCHEMA_DETECTOR_VERSION,
                score=0.95,
                severity="high",
                quantization=quantization,
                call=call,
                extra_evidence={
                    "validator": validation.violation.validator,
                    "instance_path": validation.violation.instance_path,
                },
            )
        )

    return anomalies
