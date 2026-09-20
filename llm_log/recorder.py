from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

_SECRET_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "openai-api-key",
    "anthropic-api-key",
}
_SAFE_ATOM = re.compile(r"^[a-z][a-z0-9_]*$")
_ATTRIBUTION_HEADERS = {
    "x-llm-log-company": "company",
    "x-llm-log-worker": "worker",
    "x-llm-log-agent": "agent",
    "x-llm-log-session": "session",
    "x-llm-log-task": "task",
    "x-llm-log-correlation-id": "correlation_id",
    "x-llm-log-causation-id": "causation_id",
    "x-llm-log-plan": "plan",
}


def _attribution(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        key = _ATTRIBUTION_HEADERS.get(name.lower())
        if key is not None and value:
            result[key] = value
    return result


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: "<redacted>" if name.lower() in _SECRET_HEADERS else value
        for name, value in headers.items()
    }


def _body(raw: bytes) -> dict[str, str]:
    try:
        return {"encoding": "utf-8", "text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}


def _find_model(value: Any) -> str | None:
    if isinstance(value, dict):
        model = value.get("model")
        if isinstance(model, str):
            return model
        for child in value.values():
            found = _find_model(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_model(child)
            if found is not None:
                return found
    return None


def _model(request_body: bytes) -> str | None:
    try:
        parsed = json.loads(request_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None

    found = _find_model(parsed)
    if found is not None:
        return found

    # WebSocket captures are newline-delimited frame envelopes. Text frames may
    # themselves contain JSON request objects with the selected model.
    for raw_line in request_body.splitlines():
        try:
            frame = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(frame, dict) or frame.get("type") != "text":
            continue
        text = frame.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        found = _find_model(payload)
        if found is not None:
            return found
    return None


_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "promptTokenCount",
    "inputTokens",
    "prompt_eval_count",
)
_OUTPUT_TOKEN_KEYS = (
    "output_tokens",
    "completion_tokens",
    "candidatesTokenCount",
    "outputTokens",
    "eval_count",
)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _json_documents(raw: bytes) -> list[Any]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    documents: list[Any] = []
    try:
        documents.append(json.loads(decoded))
    except json.JSONDecodeError:
        pass
    for line in decoded.splitlines():
        payload = line.strip()
        if payload.startswith("data:"):
            payload = payload[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") == "text":
            text = value.get("text")
            if isinstance(text, str):
                try:
                    documents.append(json.loads(text))
                except json.JSONDecodeError:
                    pass
        else:
            documents.append(value)
    return documents


def _token_candidates(value: Any) -> list[tuple[int | None, int | None]]:
    candidates: list[tuple[int | None, int | None]] = []
    if isinstance(value, dict):
        incoming = next(
            (_nonnegative_int(value[key]) for key in _INPUT_TOKEN_KEYS if key in value),
            None,
        )
        outgoing = next(
            (_nonnegative_int(value[key]) for key in _OUTPUT_TOKEN_KEYS if key in value),
            None,
        )
        if incoming is not None or outgoing is not None:
            candidates.append((incoming, outgoing))
        for child in value.values():
            candidates.extend(_token_candidates(child))
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_token_candidates(child))
    return candidates


def token_usage(response_body: bytes) -> tuple[int | None, int | None]:
    """Extract authoritative token counters from JSON, SSE, or WS payloads."""
    candidates = [
        candidate
        for document in _json_documents(response_body)
        for candidate in _token_candidates(document)
    ]
    incoming = [value for value, _ in candidates if value is not None]
    outgoing = [value for _, value in candidates if value is not None]
    return (max(incoming) if incoming else None, max(outgoing) if outgoing else None)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _prolog_atom(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _intent_atom(value: str) -> str:
    return value if _SAFE_ATOM.fullmatch(value) else _prolog_atom(value)


@dataclass(frozen=True, slots=True)
class WebSocketFrame:
    event_id: str
    timestamp: str
    direction: str
    frame_type: str
    payload: dict[str, str]
    payload_sha256: str

    @classmethod
    def from_bytes(
        cls,
        *,
        event_id: str,
        direction: str,
        frame_type: str,
        payload: bytes,
        timestamp: str,
    ) -> "WebSocketFrame":
        return cls(
            event_id=event_id,
            timestamp=timestamp,
            direction=direction,
            frame_type=frame_type,
            payload=_body(payload),
            payload_sha256=_sha256(payload),
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CaptureEvent:
    event_id: str
    provider: str
    upstream: str
    method: str
    path: str
    query: str
    request_headers: dict[str, str]
    request_body: dict[str, str]
    response_status: int
    response_headers: dict[str, str]
    response_body: dict[str, str]
    started_at: str
    completed_at: str
    latency_ms: int
    model: str | None
    request_sha256: str
    response_sha256: str
    intents: list[str]
    transport: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    attribution: dict[str, str] = field(default_factory=dict)
    outbound_profile: str | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        event_id: str,
        provider: str,
        upstream: str,
        method: str,
        path: str,
        query: str,
        request_headers: Mapping[str, str],
        request_body: bytes,
        response_status: int,
        response_headers: Mapping[str, str],
        response_body: bytes,
        started_at: str,
        completed_at: str,
        latency_ms: int,
        intents: Sequence[str] = (),
        transport: str = "http",
        outbound_profile: str | None = None,
    ) -> "CaptureEvent":
        input_tokens, output_tokens = token_usage(response_body)
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        return cls(
            event_id=event_id,
            provider=provider,
            upstream=upstream,
            method=method,
            path=path,
            query=query,
            request_headers=redact_headers(request_headers),
            request_body=_body(request_body),
            response_status=response_status,
            response_headers=redact_headers(response_headers),
            response_body=_body(response_body),
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            model=_model(request_body),
            request_sha256=_sha256(request_body),
            response_sha256=_sha256(response_body),
            intents=sorted(set(intents)),
            transport=transport,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            attribution=_attribution(request_headers),
            outbound_profile=outbound_profile,
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)

    def as_prolog(self) -> str:
        model = "null" if self.model is None else _prolog_atom(self.model)
        fact = (
            "llm_event("
            f"{_prolog_atom(self.event_id)}, "
            f"{_prolog_atom(self.completed_at)}, "
            f"{_prolog_atom(self.provider)}, "
            f"{model}, "
            f"{_prolog_atom(self.method)}, "
            f"{_prolog_atom(self.path)}, "
            f"{self.response_status}, "
            f"{self.latency_ms}, "
            f"{_prolog_atom(self.request_sha256)}, "
            f"{_prolog_atom(self.response_sha256)}, "
            "jsonl('events.jsonl')).\n"
        )
        transport = (
            f"transport({_prolog_atom(self.event_id)}, {_intent_atom(self.transport)}).\n"
        )
        intents = "".join(
            f"intent({_prolog_atom(self.event_id)}, {_intent_atom(label)}).\n"
            for label in self.intents
        )
        usage = ""
        if self.input_tokens is not None or self.output_tokens is not None:
            input_tokens = "null" if self.input_tokens is None else self.input_tokens
            output_tokens = "null" if self.output_tokens is None else self.output_tokens
            usage = (
                f"token_usage({_prolog_atom(self.event_id)}, {input_tokens}, "
                f"{output_tokens}).\n"
            )
        attribution = "".join(
            f"llm_attribution({_prolog_atom(self.event_id)}, {_intent_atom(key)}, {_prolog_atom(value)}).\n"
            for key, value in sorted(self.attribution.items())
        )
        outbound_profile = (
            ""
            if self.outbound_profile is None
            else f"outbound_profile({_prolog_atom(self.event_id)}, {_prolog_atom(self.outbound_profile)}).\n"
        )
        return fact + transport + usage + attribution + outbound_profile + intents


class RecorderActor:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._queue: asyncio.Queue[
            tuple[str, CaptureEvent | WebSocketFrame | None, asyncio.Future[None] | None]
        ] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._failure: BaseException | None = None

    async def start(self) -> None:
        async with self._state_lock:
            if self._task is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._task = asyncio.create_task(self._run(), name="llm-log-recorder")

    async def record(self, event: CaptureEvent) -> None:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("record", event, future))
        await future

    async def journal_frame(self, frame: WebSocketFrame) -> None:
        await self.start()
        await self._queue.put(("frame", frame, None))

    async def flush(self) -> None:
        if self._task is None:
            return
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("flush", None, future))
        await future

    async def close(self) -> None:
        async with self._state_lock:
            task = self._task
            if task is None:
                return
            future = asyncio.get_running_loop().create_future()
            await self._queue.put(("close", None, future))
            await future
            await task
            self._task = None

    async def _run(self) -> None:
        jsonl_path = self.root / "events.jsonl"
        frames_path = self.root / "frames.jsonl"
        prolog_path = self.root / "events.pl"
        with (
            jsonl_path.open("a", encoding="utf-8", buffering=1) as jsonl,
            frames_path.open("a", encoding="utf-8", buffering=1) as frames,
            prolog_path.open("a", encoding="utf-8", buffering=1) as prolog,
        ):
            while True:
                op, payload, future = await self._queue.get()
                try:
                    if self._failure is not None and op != "close":
                        raise self._failure
                    if op == "record":
                        assert isinstance(payload, CaptureEvent)
                        jsonl.write(
                            json.dumps(
                                payload.as_json(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        prolog.write(payload.as_prolog())
                        jsonl.flush()
                        prolog.flush()
                    elif op == "frame":
                        assert isinstance(payload, WebSocketFrame)
                        frames.write(
                            json.dumps(
                                payload.as_json(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        frames.flush()
                    elif op == "flush":
                        jsonl.flush()
                        frames.flush()
                        prolog.flush()
                    elif op == "close":
                        jsonl.flush()
                        frames.flush()
                        prolog.flush()
                        if future is not None:
                            future.set_result(None)
                        return
                    else:
                        raise RuntimeError(f"unknown recorder message: {op}")
                except BaseException as exc:
                    self._failure = exc
                    if future is not None and not future.done():
                        future.set_exception(exc)
                else:
                    if future is not None and not future.done():
                        future.set_result(None)
                finally:
                    self._queue.task_done()
