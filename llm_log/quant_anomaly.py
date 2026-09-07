from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class QuantDetectionConfig:
    enabled: bool = False
    low_quantizations: tuple[str, ...] = ("fp4", "int4")
    alert_unknown_on_anomaly: bool = True
    repetition_window: int = 8
    repetition_repeats: int = 4

    @classmethod
    def from_init(cls, init: Mapping[str, Any] | None) -> "QuantDetectionConfig":
        root = {} if init is None else init
        value = root.get("quant_detection", {})
        if not isinstance(value, dict):
            raise ValueError("quant_detection must be a TOML table")

        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("quant_detection.enabled must be true or false")

        low = value.get("low_quantizations", ["fp4", "int4"])
        if not isinstance(low, list) or not low or not all(isinstance(item, str) and item for item in low):
            raise ValueError("quant_detection.low_quantizations must be a non-empty string array")
        if len(set(low)) != len(low):
            raise ValueError("quant_detection.low_quantizations contains duplicates")

        alert_unknown = value.get("alert_unknown_on_anomaly", True)
        if not isinstance(alert_unknown, bool):
            raise ValueError("quant_detection.alert_unknown_on_anomaly must be true or false")

        window = value.get("repetition_window", 8)
        repeats = value.get("repetition_repeats", 4)
        if isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= 64:
            raise ValueError("quant_detection.repetition_window must be an integer from 2 to 64")
        if isinstance(repeats, bool) or not isinstance(repeats, int) or not 2 <= repeats <= 32:
            raise ValueError("quant_detection.repetition_repeats must be an integer from 2 to 32")

        return cls(
            enabled=enabled,
            low_quantizations=tuple(low),
            alert_unknown_on_anomaly=alert_unknown,
            repetition_window=window,
            repetition_repeats=repeats,
        )


@dataclass(frozen=True, slots=True)
class OutputAnomaly:
    detector: str
    severity: str
    score: float

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _payloads(response_body: bytes):
    try:
        payload = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        yield payload

    for raw_line in response_body.splitlines():
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        encoded = line[5:].strip()
        if not encoded or encoded == b"[DONE]":
            continue
        try:
            payload = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            yield payload


def _request_tools(request_body: bytes) -> set[str]:
    try:
        payload = json.loads(request_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()

    names: set[str] = set()
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _response_material(response_body: bytes) -> tuple[str, dict[int, dict[str, str]]]:
    text: list[str] = []
    tool_calls: dict[int, dict[str, str]] = {}

    for payload in _payloads(response_body):
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("delta") or choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for key in ("content", "reasoning", "reasoning_content"):
                part = message.get(key)
                if isinstance(part, str):
                    text.append(part)
            for position, tool_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                index = tool_call.get("index", position)
                if isinstance(index, bool) or not isinstance(index, int):
                    index = position
                current = tool_calls.setdefault(index, {"name": "", "arguments": ""})
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                arguments = function.get("arguments")
                if isinstance(name, str) and name:
                    current["name"] = name
                if isinstance(arguments, str):
                    current["arguments"] += arguments

    return "".join(text), tool_calls


def _has_repetition_loop(text: str, *, window: int, repeats: int) -> bool:
    tokens = re.findall(r"\S+", text)
    required = window * repeats
    if len(tokens) < required:
        return False
    for start in range(0, len(tokens) - required + 1):
        block = tokens[start : start + window]
        if all(
            tokens[start + offset * window : start + (offset + 1) * window] == block
            for offset in range(1, repeats)
        ):
            return True
    return False


def detect_output_anomalies(
    request_body: bytes,
    response_body: bytes,
    config: QuantDetectionConfig,
) -> list[OutputAnomaly]:
    if not config.enabled:
        return []

    anomalies: list[OutputAnomaly] = []
    try:
        response_body.decode("utf-8")
    except UnicodeDecodeError:
        anomalies.append(OutputAnomaly("invalid_utf8", "high", 0.98))

    text, tool_calls = _response_material(response_body)
    tools = _request_tools(request_body)

    if "\ufffd" in text:
        anomalies.append(OutputAnomaly("replacement_character", "high", 0.95))
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        anomalies.append(OutputAnomaly("unexpected_control_character", "high", 0.90))
    if _has_repetition_loop(
        text,
        window=config.repetition_window,
        repeats=config.repetition_repeats,
    ):
        anomalies.append(OutputAnomaly("repetition_loop", "high", 0.85))

    for tool_call in tool_calls.values():
        name = tool_call["name"]
        arguments = tool_call["arguments"]
        if tools and name and name not in tools:
            anomalies.append(OutputAnomaly("unknown_tool_name", "high", 0.92))
        if arguments:
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError:
                anomalies.append(OutputAnomaly("invalid_tool_arguments_json", "high", 0.96))
            else:
                if not isinstance(decoded, dict):
                    anomalies.append(OutputAnomaly("tool_arguments_not_object", "medium", 0.75))

    unique: dict[str, OutputAnomaly] = {}
    for anomaly in anomalies:
        previous = unique.get(anomaly.detector)
        if previous is None or anomaly.score > previous.score:
            unique[anomaly.detector] = anomaly
    return list(unique.values())


def alert_category(
    quantization: str,
    anomalies: list[OutputAnomaly],
    config: QuantDetectionConfig,
) -> str | None:
    if not config.enabled:
        return None
    if quantization in config.low_quantizations:
        return "low_quantization"
    if quantization == "unknown" and anomalies and config.alert_unknown_on_anomaly:
        return "possible_quantization_or_model_anomaly"
    if anomalies:
        return "model_output_anomaly"
    return None
