from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Mapping

from .openrouter_observability import observe_openrouter_response
from .quant_anomaly import QuantDetectionConfig, alert_category, detect_output_anomalies
from .recorder import CaptureEvent, RecorderActor


def _captured_body_bytes(body: dict[str, str]) -> bytes:
    if body.get("encoding") == "utf-8":
        return body.get("text", "").encode("utf-8")
    if body.get("encoding") == "base64":
        return base64.b64decode(body.get("data", ""), validate=True)
    return b""


class RoutingRecorder(RecorderActor):
    """Recorder that adds provenance-safe routing and anomaly observations.

    Raw request/response evidence remains owned by RecorderActor. Derived routing,
    anomaly and alert records live in separate JSONL streams so detector revisions
    never rewrite the append-only capture schema.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        init: Mapping[str, Any] | None = None,
    ):
        super().__init__(root)
        self.quant_detection = QuantDetectionConfig.from_init(init)
        self._derived_lock = asyncio.Lock()

    async def _append(self, filename: str, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._derived_lock:
            path = self.root / filename
            with path.open("a", encoding="utf-8", buffering=1) as handle:
                handle.write(encoded)

    async def record(self, event: CaptureEvent) -> None:
        await super().record(event)
        request_body = _captured_body_bytes(event.request_body)
        response_body = _captured_body_bytes(event.response_body)
        observation = observe_openrouter_response(event.provider, response_body)

        if observation:
            await self._append(
                "routing.jsonl",
                {
                    "event_id": event.event_id,
                    "completed_at": event.completed_at,
                    "model": event.model,
                    **observation,
                },
            )

        anomalies = detect_output_anomalies(
            request_body,
            response_body,
            self.quant_detection,
        )
        if anomalies:
            await self._append(
                "anomalies.jsonl",
                {
                    "event_id": event.event_id,
                    "completed_at": event.completed_at,
                    "provider": event.provider,
                    "selected_provider": observation.get("selected_provider") if observation else None,
                    "model": event.model,
                    "quantization": observation.get("quantization", "unknown") if observation else "unknown",
                    "detectors": [anomaly.as_json() for anomaly in anomalies],
                },
            )

        quantization = observation.get("quantization", "unknown") if observation else "unknown"
        category = alert_category(quantization, anomalies, self.quant_detection)
        if category == "possible_quantization_or_model_anomaly" and event.provider != "openrouter":
            category = "model_output_anomaly"
        if category is None:
            return

        severity = "high" if any(anomaly.severity == "high" for anomaly in anomalies) else "medium"
        if category == "low_quantization":
            severity = "high"
        await self._append(
            "alerts.jsonl",
            {
                "event_id": event.event_id,
                "completed_at": event.completed_at,
                "severity": severity,
                "category": category,
                "provider": event.provider,
                "selected_provider": observation.get("selected_provider") if observation else None,
                "model": event.model,
                "quantization": quantization,
                "detectors": [anomaly.detector for anomaly in anomalies],
            },
        )
