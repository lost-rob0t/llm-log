from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from .openrouter_observability import observe_openrouter_response
from .recorder import CaptureEvent, RecorderActor


def _captured_body_bytes(body: dict[str, str]) -> bytes:
    if body.get("encoding") == "utf-8":
        return body.get("text", "").encode("utf-8")
    if body.get("encoding") == "base64":
        return base64.b64decode(body.get("data", ""), validate=True)
    return b""


class RoutingRecorder(RecorderActor):
    """Recorder that adds provenance-safe provider/quantization observations.

    Raw request/response evidence remains owned by RecorderActor. Routing facts are
    derived into a separate JSONL stream so they can evolve without rewriting the
    append-only capture schema.
    """

    def __init__(self, root: str | Path):
        super().__init__(root)
        self._routing_lock = asyncio.Lock()

    async def record(self, event: CaptureEvent) -> None:
        await super().record(event)
        observation = observe_openrouter_response(
            event.provider,
            _captured_body_bytes(event.response_body),
        )
        if not observation:
            return

        record = {
            "event_id": event.event_id,
            "completed_at": event.completed_at,
            "model": event.model,
            **observation,
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._routing_lock:
            path = self.root / "routing.jsonl"
            with path.open("a", encoding="utf-8", buffering=1) as handle:
                handle.write(encoded)
