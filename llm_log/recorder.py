from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .privacy import bounded_identifier, credential_values, redact_bytes, redact_headers, redact_known, redact_text, redact_value

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
        if key is not None and bounded_identifier(value) is not None:
            result[key] = value
    return result


def _body(raw: bytes) -> dict[str, str]:
    raw = redact_bytes(raw)
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
    if not isinstance(value, (int, str)):
        return None
    if isinstance(value, str) and not value.isascii():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
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


_USAGE_KEYS = {
    "cache_read_input_tokens": ("cache_read_input_tokens", "cached_tokens", "cachedContentTokenCount", "cacheReadInputTokens"),
    "cache_creation_input_tokens": ("cache_creation_input_tokens", "cache_write_input_tokens", "cacheWriteInputTokens"),
    "reasoning_tokens": ("reasoning_tokens", "thoughtsTokenCount", "reasoningTokenCount"),
    "reported_total_tokens": ("total_tokens", "totalTokenCount", "totalTokens"),
}


def provider_usage(response_body: bytes) -> dict[str, int | None]:
    """Provider-reported counters; cumulative stream observations use maxima.

    Cache/reasoning counters are components of provider usage, never added to it.
    No total is inferred here. token_usage retains its public two-counter API.
    """
    result: dict[str, int | None] = {key: None for key in _USAGE_KEYS}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for field_name, aliases in _USAGE_KEYS.items():
                for alias in aliases:
                    count = _nonnegative_int(value.get(alias))
                    if count is not None:
                        prior = result[field_name]
                        result[field_name] = max(prior, count) if prior is not None else count
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for document in _json_documents(response_body):
        visit(document)
    return result


_METADATA_HEADERS = {"request-id", "x-request-id", "openai-request-id", "anthropic-request-id",
                     "retry-after", "retry-after-ms", "x-ms-request-id"}
for _prefix in ("x-ratelimit", "anthropic-ratelimit"):
    for _metric in ("requests", "tokens", "input-tokens", "output-tokens"):
        for _counter in ("limit", "remaining", "reset"):
            _METADATA_HEADERS.add(f"{_prefix}-{_counter}-{_metric}")
            _METADATA_HEADERS.add(f"{_prefix}-{_metric}-{_counter}")


_COUNTER_FIELDS = {
    "limit", "remaining", "used", "total", "available", "balance", "amount",
    "used_percent", "remaining_percent", "reset_at", "reset_after", "reset_seconds",
    "reset_in_seconds", "window_seconds", "total_credits", "remaining_credits",
    "used_credits", "total_usage", "requests", "tokens", "input_tokens", "output_tokens",
}
_COUNTER_GROUPS = {"quota", "quotas", "credits", "credit", "rate_limits"}
_COUNTER_DIMENSIONS = {"requests", "tokens", "input_tokens", "output_tokens", "credits",
                       "primary", "secondary", "daily", "monthly"}


def _json_counter_metadata(response: Mapping[str, Any]) -> dict[str, int | float]:
    """Only finite numeric provider counters in named quota/credit containers.

    Fixed names, depth and result bounds prevent arbitrary provider content from
    being promoted into metadata. Missing or malformed values are omitted.
    """
    result: dict[str, int | float] = {}

    def number(value: Any) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and 0 <= value <= 10**18 and math.isfinite(value))

    def counters(value: Any, path: str, depth: int = 0) -> None:
        if depth > 3 or len(result) >= 64:
            return
        if number(value):
            result[path] = value
        elif isinstance(value, dict):
            for key in sorted(_COUNTER_FIELDS | _COUNTER_DIMENSIONS):
                child = value.get(key)
                if number(child):
                    result[f"{path}.{key}"] = child
                elif key in _COUNTER_DIMENSIONS and isinstance(child, dict):
                    counters(child, f"{path}.{key}", depth + 1)
                if len(result) >= 64:
                    return
        elif isinstance(value, list):
            for index, child in enumerate(value[:16]):
                if isinstance(child, dict):
                    counters(child, f"{path}.{index}", depth + 1)

    def envelopes(value: Mapping[str, Any], depth: int = 0) -> None:
        for key in sorted(_COUNTER_GROUPS):
            if key in value:
                counters(value[key], key)
        if depth < 2:
            for key in ("data", "usage", "meta", "metadata"):
                child = value.get(key)
                if isinstance(child, dict):
                    envelopes(child, depth + 1)

    envelopes(response)
    return result


def provider_metadata(headers: Mapping[str, str], response_body: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {name.lower(): value for name, value in headers.items()
              if name.lower() in _METADATA_HEADERS and bounded_identifier(value) is not None}
    for document in _json_documents(response_body):
        if not isinstance(document, dict):
            continue
        # IDs come only from documented response envelopes, never arbitrary message content.
        response = document.get("response", document)
        if isinstance(response, dict):
            result.update(_json_counter_metadata(response))
            for key in ("id", "request_id", "response_id"):
                value = bounded_identifier(response.get(key))
                if value is not None:
                    result[key] = value
    return result


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _prolog_atom(value: str) -> str:
    return "'" + redact_text(value).replace("'", "''") + "'"


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
            payload_sha256=_sha256(redact_bytes(payload)),
        )

    def as_json(self) -> dict[str, Any]:
        return redact_value(asdict(self))


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
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    reported_total_tokens: int | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    final_provider: str | None = None
    redaction_version: str = "credential-filter-v1"

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
        attempts: Sequence[Mapping[str, Any]] = (),
        final_provider: str | None = None,
        attribution: Mapping[str, str] | None = None,
    ) -> "CaptureEvent":
        input_tokens, output_tokens = token_usage(response_body)
        rich_usage = provider_usage(response_body)
        total_tokens = rich_usage["reported_total_tokens"]
        if total_tokens is None:
            # Compatibility field: derive only from known input and output.
            # reported_total_tokens remains None unless explicitly supplied.
            total_tokens = (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            )
        secrets = credential_values([dict(request_headers), dict(response_headers),
                                     request_body, response_body, query, upstream])
        request_body = redact_bytes(redact_known(request_body, secrets))
        response_body = redact_bytes(redact_known(response_body, secrets))
        upstream, path, query = (redact_known(value, secrets) for value in (upstream, path, query))
        request_headers = redact_known(dict(request_headers), secrets)
        response_headers = redact_known(dict(response_headers), secrets)
        return cls(
            event_id=event_id,
            provider=provider,
            upstream=redact_text(upstream),
            method=method,
            path=redact_text(path),
            query=redact_text(query),
            request_headers=redact_headers(request_headers),
            request_body=_body(request_body),
            response_status=response_status,
            response_headers=redact_headers(response_headers),
            response_body=_body(response_body),
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            model=redact_text(_model(request_body)) if _model(request_body) is not None else None,
            request_sha256=_sha256(redact_bytes(request_body)),
            response_sha256=_sha256(redact_bytes(response_body)),
            intents=sorted(set(intents)),
            transport=transport,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            attribution=(_attribution(request_headers) if attribution is None else
                         {key: value for key, value in attribution.items()
                          if key in {*_ATTRIBUTION_HEADERS.values(), "trust"} and bounded_identifier(value) is not None}),
            **rich_usage,
            provider_metadata=provider_metadata(response_headers, response_body),
            attempts=redact_value(redact_known(list(attempts), secrets)),
            final_provider=bounded_identifier(final_provider),
        )

    def as_json(self) -> dict[str, Any]:
        return redact_value(asdict(self))

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
        return fact + transport + usage + attribution + intents


class RecorderActor:
    def __init__(self, root: str | Path, *, retention: str = "full"):
        if retention not in {"full", "metadata"}:
            raise ValueError("retention must be full or metadata")
        self.retention = retention
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

    def prepare(self, event: CaptureEvent) -> CaptureEvent:
        """Apply retention before persistence or downstream expert ingestion.

        Hashes always describe the redacted full capture, including when body
        retention is disabled. Usage extraction occurs before retention.
        """
        if self.retention == "metadata":
            return replace(event, request_body={"encoding": "omitted"},
                           response_body={"encoding": "omitted"})
        return event

    async def record(self, event: CaptureEvent) -> None:
        event = self.prepare(event)
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("record", event, future))
        await future

    async def journal_frame(self, frame: WebSocketFrame) -> None:
        if self.retention == "metadata":
            frame = replace(frame, payload={"encoding": "omitted"})
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
