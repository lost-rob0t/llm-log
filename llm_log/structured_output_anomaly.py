from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .schema_validation import schema_is_usable, validate_instance

_JSON_DETECTOR_VERSION = "structured-output-json/1"
_SCHEMA_DETECTOR_VERSION = "structured-output-schema/1"
_MAX_OUTPUTS = 128
_MAX_QUANTIZATION_LENGTH = 64


@dataclass(frozen=True, slots=True)
class StructuredOutputContract:
    surface: str
    schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReconstructedStructuredOutput:
    index: int
    text: str
    completed: bool
    refusal: bool


@dataclass(frozen=True, slots=True)
class StructuredOutputAnomaly:
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
    source: str = "structured_output"

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _safe_string(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _quantization(value: str | None) -> str:
    if value is None:
        return "unknown"
    safe = _safe_string(value, limit=_MAX_QUANTIZATION_LENGTH)
    return safe if safe is not None else "unknown"


def _request_object(request_body: bytes) -> Mapping[str, Any] | None:
    try:
        value = json.loads(request_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def structured_output_contract(request_body: bytes) -> StructuredOutputContract | None:
    request = _request_object(request_body)
    if request is None:
        return None

    response_format = request.get("response_format")
    if isinstance(response_format, Mapping) and response_format.get("type") == "json_schema":
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, Mapping):
            schema = json_schema.get("schema")
            if isinstance(schema, Mapping):
                return StructuredOutputContract(
                    surface="response_format.json_schema",
                    schema=schema,
                )

    text = request.get("text")
    if isinstance(text, Mapping):
        format_value = text.get("format")
        if isinstance(format_value, Mapping) and format_value.get("type") == "json_schema":
            schema = format_value.get("schema")
            if isinstance(schema, Mapping):
                return StructuredOutputContract(surface="text.format", schema=schema)

    return None


def _json_or_sse_documents(response_body: bytes) -> list[Mapping[str, Any]]:
    try:
        text = response_body.decode("utf-8")
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
        else:
            continue
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            documents.append(parsed)
    return documents


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _has_refusal(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return value is not None and value is not False


def _chat_outputs(documents: list[Mapping[str, Any]]) -> list[ReconstructedStructuredOutput]:
    states: dict[int, dict[str, Any]] = {}

    for document in documents:
        choices = document.get("choices")
        if not isinstance(choices, list):
            continue
        for ordinal, choice in enumerate(choices[:_MAX_OUTPUTS]):
            if not isinstance(choice, Mapping):
                continue
            raw_index = choice.get("index")
            index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else ordinal
            if index < 0 or index >= _MAX_OUTPUTS:
                continue
            state = states.setdefault(
                index,
                {"parts": [], "refusal": False, "finish_reason": None},
            )

            message = choice.get("message")
            if isinstance(message, Mapping):
                text = _content_text(message.get("content"))
                if text:
                    state["parts"] = [text]
                if _has_refusal(message.get("refusal")):
                    state["refusal"] = True

            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                text = _content_text(delta.get("content"))
                if text:
                    state["parts"].append(text)
                if _has_refusal(delta.get("refusal")):
                    state["refusal"] = True

            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                state["finish_reason"] = finish_reason

    return [
        ReconstructedStructuredOutput(
            index=index,
            text="".join(state["parts"]),
            completed=state["finish_reason"] == "stop",
            refusal=bool(state["refusal"]),
        )
        for index, state in sorted(states.items())
    ]


def _response_output_messages(response: Mapping[str, Any]) -> list[ReconstructedStructuredOutput]:
    if response.get("status") != "completed":
        return []
    output = response.get("output")
    if not isinstance(output, list):
        return []

    results: list[ReconstructedStructuredOutput] = []
    for index, item in enumerate(output[:_MAX_OUTPUTS]):
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if item.get("status") not in (None, "completed"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        parts: list[str] = []
        refusal = False
        for part in content:
            if not isinstance(part, Mapping):
                continue
            part_type = part.get("type")
            if part_type == "refusal":
                refusal = True
                continue
            if part_type == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        results.append(
            ReconstructedStructuredOutput(
                index=index,
                text="".join(parts),
                completed=True,
                refusal=refusal,
            )
        )
    return results


def _responses_outputs(documents: list[Mapping[str, Any]]) -> list[ReconstructedStructuredOutput]:
    if len(documents) == 1 and documents[0].get("object") == "response":
        return _response_output_messages(documents[0])

    states: dict[int, dict[str, Any]] = {}
    terminal_state: str | None = None
    terminal_response: Mapping[str, Any] | None = None

    for event in documents:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue

        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            terminal_state = event_type
            response = event.get("response")
            if isinstance(response, Mapping):
                terminal_response = response
            continue

        raw_index = event.get("output_index")
        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
        if index < 0 or index >= _MAX_OUTPUTS:
            continue
        state = states.setdefault(index, {"parts": [], "done_text": None, "refusal": False})

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                state["parts"].append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str):
                state["done_text"] = text
        elif event_type.startswith("response.refusal."):
            state["refusal"] = True

    if terminal_state != "response.completed":
        return []

    if not states and terminal_response is not None:
        return _response_output_messages(terminal_response)

    results: list[ReconstructedStructuredOutput] = []
    for index, state in sorted(states.items()):
        text = "".join(state["parts"])
        if not text and isinstance(state["done_text"], str):
            text = state["done_text"]
        results.append(
            ReconstructedStructuredOutput(
                index=index,
                text=text,
                completed=True,
                refusal=bool(state["refusal"]),
            )
        )
    return results


def _anomaly(
    *,
    event_id: str,
    provider: str,
    model: str,
    output: ReconstructedStructuredOutput,
    contract: StructuredOutputContract,
    detector_id: str,
    detector_version: str,
    score: float,
    quantization: str,
    extra_evidence: Mapping[str, Any] | None = None,
) -> StructuredOutputAnomaly:
    evidence: dict[str, Any] = {
        "contract_surface": contract.surface,
        "output_index": output.index,
        "output_length": len(output.text),
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    return StructuredOutputAnomaly(
        anomaly_id=f"anomaly:{event_id}:{detector_id}:{output.index}",
        event_id=event_id,
        provider=provider,
        model=model,
        detector_id=detector_id,
        detector_version=detector_version,
        domain="model_behavior",
        score=score,
        severity="high",
        quantization=quantization,
        evidence=evidence,
    )


def analyze_structured_output(
    *,
    event_id: str,
    provider: str,
    model: str,
    request_body: bytes,
    response_body: bytes,
    observed_quantization: str | None = None,
) -> list[StructuredOutputAnomaly]:
    contract = structured_output_contract(request_body)
    if contract is None or not schema_is_usable(contract.schema):
        return []

    documents = _json_or_sse_documents(response_body)
    outputs = (
        _chat_outputs(documents)
        if contract.surface == "response_format.json_schema"
        else _responses_outputs(documents)
    )
    quantization = _quantization(observed_quantization)
    anomalies: list[StructuredOutputAnomaly] = []

    for output in outputs:
        if not output.completed or output.refusal:
            continue
        try:
            instance = json.loads(output.text)
        except json.JSONDecodeError:
            anomalies.append(
                _anomaly(
                    event_id=event_id,
                    provider=provider,
                    model=model,
                    output=output,
                    contract=contract,
                    detector_id="structured_output_invalid_json",
                    detector_version=_JSON_DETECTOR_VERSION,
                    score=1.0,
                    quantization=quantization,
                    extra_evidence={"validation": "json_decode_failed"},
                )
            )
            continue

        validation = validate_instance(contract.schema, instance)
        if validation.state != "violation" or validation.violation is None:
            continue
        anomalies.append(
            _anomaly(
                event_id=event_id,
                provider=provider,
                model=model,
                output=output,
                contract=contract,
                detector_id="structured_output_schema_violation",
                detector_version=_SCHEMA_DETECTOR_VERSION,
                score=0.95,
                quantization=quantization,
                extra_evidence={
                    "validator": validation.violation.validator,
                    "instance_path": validation.violation.instance_path,
                },
            )
        )

    return anomalies
