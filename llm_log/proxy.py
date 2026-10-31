from __future__ import annotations

import asyncio
import base64
import json
import math
import hmac
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Mapping, Protocol

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web

from .analytics import install_analytics_routes
from .expert_adapter import SubprocessExpertPlane
from .recorder import CaptureEvent, RecorderActor, WebSocketFrame, token_usage, provider_usage, provider_metadata
from .privacy import credential_values, redact_known
from .routing import FALLBACK_VIA_MARKER, FallbackPolicy, ProviderPolicyConflict, account_scope, classify_exhaustion

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_INTERNAL_ATTRIBUTION_HEADERS = {
    "x-llm-log-company",
    "x-llm-log-worker",
    "x-llm-log-agent",
    "x-llm-log-session",
    "x-llm-log-task",
    "x-llm-log-correlation-id",
    "x-llm-log-causation-id",
    "x-llm-log-plan",
}
_REQUEST_DROP = _HOP_BY_HOP | {"host", "content-length"} | _INTERNAL_ATTRIBUTION_HEADERS
_RESPONSE_DROP = _HOP_BY_HOP
_WS_REQUEST_DROP = _REQUEST_DROP | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}
_SESSION_KEY = web.AppKey("session", ClientSession)
_RECORDER_KEY = web.AppKey("recorder", RecorderActor)


class Classifier(Protocol):
    def classify(self, text: str) -> list[str]: ...


class ExpertPlane(Protocol):
    async def health(self) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _REQUEST_DROP and not name.lower().startswith("x-llm-log-")}


def _websocket_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _WS_REQUEST_DROP and not name.lower().startswith("x-llm-log-")
    }


def _response_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in headers.items() if name.lower() not in _RESPONSE_DROP]


def _websocket_protocols(headers: Mapping[str, str]) -> tuple[str, ...]:
    raw = headers.get("Sec-WebSocket-Protocol", "")
    return tuple(protocol.strip() for protocol in raw.split(",") if protocol.strip())


def _websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url.removeprefix("https://")
    if url.startswith("http://"):
        return "ws://" + url.removeprefix("http://")
    return url


def _websocket_frame(message_type: WSMsgType, data: str | bytes) -> bytes:
    if message_type == WSMsgType.TEXT:
        frame = {"type": "text", "text": data}
    elif message_type == WSMsgType.BINARY:
        assert isinstance(data, bytes)
        frame = {
            "type": "binary",
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
        }
    else:
        raise ValueError(f"unsupported websocket capture type: {message_type}")
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def _classify(classifier: Classifier | None, request_body: bytes) -> list[str]:
    if classifier is None:
        return []
    text = request_body.decode("utf-8", errors="replace")
    try:
        return await asyncio.to_thread(classifier.classify, text)
    except Exception:
        return []


async def _enforce_expert_policy(
    expert_plane: ExpertPlane | None,
    *,
    require_expert_plane: bool,
) -> None:
    if expert_plane is None:
        if require_expert_plane:
            raise web.HTTPServiceUnavailable(text="expert plane unavailable")
        return

    try:
        await expert_plane.health()
    except Exception:
        if require_expert_plane:
            raise web.HTTPServiceUnavailable(text="expert plane unavailable") from None


_INGEST_TASKS_KEY = web.AppKey("expert-ingest-tasks", set)
_USER_MESSAGE_LIMIT = 4000


def _user_message_from_api_body(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def _extract_user_message(request_body: bytes) -> str:
    """Extract the last user message from one captured request body.

    Handles JSON API bodies and newline-delimited WebSocket frame envelopes;
    returns "" when no user text can be found.
    """
    text = request_body.decode("utf-8", errors="replace")
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    if isinstance(body, dict) and "messages" in body:
        return _user_message_from_api_body(body)[:_USER_MESSAGE_LIMIT]
    for line in text.splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict) and frame.get("type") == "text":
            inner = frame.get("text")
            if not isinstance(inner, str):
                continue
            try:
                parsed = json.loads(inner)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return _user_message_from_api_body(parsed)[:_USER_MESSAGE_LIMIT]
    return ""


def _expert_base_payload(event: CaptureEvent) -> dict:
    return {
        "provider": event.provider,
        "model": event.model or "unknown",
        "transport": event.transport,
        "started_at": event.started_at,
        "completed_at": event.completed_at,
        "request_sha256": event.request_sha256,
        "response_sha256": event.response_sha256,
    }


async def _expert_ingest(expert_plane, event: CaptureEvent, request_body: bytes) -> None:
    try:
        payload = _expert_base_payload(event)
        await expert_plane.observe_request(
            event_id=event.event_id,
            payload=payload,
            session_id="proxy",
            task_id="proxy",
        )
        from .expert_capture import expert_usage_payload
        if hasattr(expert_plane, "observe_usage"):
            usage = expert_usage_payload(event.as_json())
            if usage is not None:
                await expert_plane.observe_usage(event_id=event.event_id, payload=usage, session_id="proxy", task_id="proxy")
        if hasattr(expert_plane, "record_outcome_evidence"):
            from .session_mining import replay_mined_failures
            await replay_mined_failures(expert_plane, event.as_json())
        safe_body = event.request_body.get("text", "").encode("utf-8")
        message = _extract_user_message(safe_body)
        if message:
            await expert_plane.classify_request(
                event_id=event.event_id,
                payload={
                    **payload,
                    "message": message,
                    "user_message_id": "um-" + event.event_id[:8],
                    "request_id": event.event_id,
                    "client": "proxy",
                },
                session_id="proxy",
                task_id="proxy",
            )
    except Exception:
        return


def _schedule_expert_ingest(
    app: web.Application,
    expert_plane,
    event: CaptureEvent,
    request_body: bytes,
) -> None:
    if expert_plane is None or not hasattr(expert_plane, "observe_request"):
        return
    event = app[_RECORDER_KEY].prepare(event)
    tasks = app[_INGEST_TASKS_KEY]
    task = asyncio.create_task(
        _expert_ingest(expert_plane, event, request_body),
        name="llm-log-expert-ingest",
    )
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _relay_websocket(
    source,
    target,
    captured: bytearray,
    *,
    recorder: RecorderActor,
    event_id: str,
    direction: str,
    known_secrets: set[str],
) -> None:
    async for message in source:
        if message.type == WSMsgType.TEXT:
            raw = message.data.encode("utf-8")
            captured.extend(_websocket_frame(message.type, message.data))
            known_secrets.update(credential_values(raw))
            await recorder.journal_frame(
                WebSocketFrame.from_bytes(
                    event_id=event_id,
                    direction=direction,
                    frame_type="text",
                    payload=redact_known(raw, known_secrets),
                    timestamp=_now(),
                )
            )
            await target.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            captured.extend(_websocket_frame(message.type, message.data))
            known_secrets.update(credential_values(message.data))
            await recorder.journal_frame(
                WebSocketFrame.from_bytes(
                    event_id=event_id,
                    direction=direction,
                    frame_type="binary",
                    payload=redact_known(message.data, known_secrets),
                    timestamp=_now(),
                )
            )
            await target.send_bytes(message.data)
        elif message.type == WSMsgType.ERROR:
            error = source.exception()
            if error is not None:
                raise error
            raise RuntimeError("websocket relay failed")
    error = source.exception()
    if error is not None:
        raise error
    if getattr(source, "close_code", None) not in (None, 1000, 1001):
        raise RuntimeError("websocket closed abnormally")


async def _proxy_websocket(
    request: web.Request,
    *,
    provider: str,
    upstream: str,
    upstream_url: str,
    tail: str,
    recorder: RecorderActor,
    classifier: Classifier | None,
    session: ClientSession,
    expert_plane: ExpertPlane | None,
    attribution: Mapping[str, str],
) -> web.StreamResponse:
    event_id = str(uuid.uuid4())
    started_at = _now()
    started = time.perf_counter()
    client_frames = bytearray()
    server_frames = bytearray()
    protocols = _websocket_protocols(request.headers)
    known_secrets = credential_values(dict(request.headers))

    try:
        upstream_socket = await session.ws_connect(
            _websocket_url(upstream_url),
            headers=_websocket_request_headers(request.headers),
            protocols=protocols,
            autoping=True,
        )
    except Exception as exc:
        completed_at = _now()
        failure = str(exc).encode("utf-8", errors="replace")
        event = CaptureEvent.from_bytes(
            event_id=event_id,
            provider=provider,
            upstream=upstream,
            method=request.method,
            path="/" + tail,
            query=request.query_string,
            request_headers=request.headers,
            request_body=b"",
            response_status=502,
            response_headers={},
            response_body=failure,
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=round((time.perf_counter() - started) * 1000),
            transport="websocket",
            attribution=attribution,
            final_provider=provider,
            attempts=[_attempt_record(number=1, provider=provider, model=None, status=502,
                reason="transport_error", body=failure, headers={}, started_at=started_at,
                committed=False, terminal_class="transport_failure", policy=None)],
        )
        await recorder.record(event)
        _schedule_expert_ingest(request.app, expert_plane, event, b"")
        return web.Response(status=502, text="upstream websocket connection failed")

    selected_protocol = upstream_socket.protocol
    downstream = web.WebSocketResponse(
        protocols=(selected_protocol,) if selected_protocol is not None else (),
        autoping=True,
    )

    tasks = set()
    relay_failed = False
    committed = False
    cancelled = None
    try:
        await downstream.prepare(request)
        committed = True
        client_to_upstream = asyncio.create_task(
            _relay_websocket(
                downstream,
                upstream_socket,
                client_frames,
                recorder=recorder,
                event_id=event_id,
                direction="client_to_upstream",
                known_secrets=known_secrets,
            ),
            name="llm-log-ws-client-to-upstream",
        )
        upstream_to_client = asyncio.create_task(
            _relay_websocket(
                upstream_socket,
                downstream,
                server_frames,
                recorder=recorder,
                event_id=event_id,
                direction="upstream_to_client",
                known_secrets=known_secrets,
            ),
            name="llm-log-ws-upstream-to-client",
        )
        tasks = {client_to_upstream, upstream_to_client}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (Exception, asyncio.CancelledError) as exc:
        relay_failed = True
        if isinstance(exc, asyncio.CancelledError):
            cancelled = exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    request_body = bytes(client_frames)
    response_body = bytes(server_frames)
    intents = await _classify(classifier, request_body)
    completed_at = _now()
    response_headers = (
        {"Sec-WebSocket-Protocol": selected_protocol}
        if selected_protocol is not None
        else {}
    )
    event = CaptureEvent.from_bytes(
        event_id=event_id,
        provider=provider,
        upstream=upstream,
        method=request.method,
        path="/" + tail,
        query=request.query_string,
        request_headers=request.headers,
        request_body=request_body,
        response_status=101 if committed else 502,
        response_headers=response_headers,
        response_body=response_body,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=round((time.perf_counter() - started) * 1000),
        intents=intents,
        transport="websocket",
        attribution=attribution,
        final_provider=provider,
        attempts=[_attempt_record(number=1, provider=provider, model=None,
            status=101 if committed else 502,
            reason=("midstream_failure" if committed else "transport_error") if relay_failed else None,
            body=response_body, headers=response_headers, started_at=started_at,
            committed=committed,
            terminal_class=("midstream_failure" if committed else "transport_failure") if relay_failed else "success",
            policy=None)],
    )
    try:
        await recorder.record(event)
        _schedule_expert_ingest(request.app, expert_plane, event, bytes(client_frames))
    finally:
        await asyncio.gather(upstream_socket.close(), downstream.close(), return_exceptions=True)
    if cancelled is not None:
        raise cancelled
    if not committed:
        return web.Response(status=502, text="upstream websocket relay failed")
    return downstream


def _reported_cost(body: bytes) -> dict | None:
    try:
        documents = [json.loads(body)]
    except (ValueError, UnicodeError):
        documents = []
        for line in body.splitlines():
            line = line.removeprefix(b"data:").strip()
            try:
                documents.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
    costs = []

    def visit(value):
        if isinstance(value, dict):
            usage = value.get("usage")
            if isinstance(usage, dict):
                cost = usage.get("cost")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    try:
                        if math.isfinite(cost) and cost >= 0:
                            costs.append(cost)
                    except OverflowError:
                        pass
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for document in documents:
        visit(document)
    return {"amount": costs[-1], "currency": None, "source": "usage.cost"} if costs else None


def _attempt_record(*, number: int, provider: str, model: str | None, status: int,
                    reason: str | None, body: bytes, headers: Mapping[str, str],
                    started_at: str, committed: bool, terminal_class: str,
                    policy: FallbackPolicy | None) -> dict:
    incoming, outgoing = token_usage(body)
    usage = provider_usage(body)
    total = usage.get("reported_total_tokens")
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    return dict(attempt_number=number, provider=provider, model=model, status=status,
                reason=reason, input_tokens=incoming, output_tokens=outgoing, total_tokens=total,
                **usage, provider_metadata=provider_metadata(headers, body),
                started_at=started_at, completed_at=_now(), downstream_committed=committed,
                terminal_class=terminal_class, policy_version=policy.policy_version if policy else None,
                rule_version="exhaustion-v1", cost=_reported_cost(body))


def build_app(
    upstreams: Mapping[str, str],
    recorder: RecorderActor,
    classifier: Classifier | None,
    *,
    timeout_seconds: float = 600.0,
    expert_plane: ExpertPlane | None = None,
    require_expert_plane: bool = False,
    fallback_policy: FallbackPolicy | None = None,
    trusted_attribution_token_env: str | None = None,
    trusted_attribution: Mapping[str, str] | None = None,
) -> web.Application:
    normalized = {name: url.rstrip("/") for name, url in upstreams.items()}
    app = web.Application(client_max_size=1024**3)
    app[_RECORDER_KEY] = recorder

    async def startup(application: web.Application) -> None:
        await recorder.start()
        application[_SESSION_KEY] = ClientSession(
            timeout=ClientTimeout(total=timeout_seconds),
            auto_decompress=False,
            cookie_jar=DummyCookieJar(),
        )
        application[_INGEST_TASKS_KEY] = set()
        if isinstance(expert_plane, SubprocessExpertPlane):
            try:
                await expert_plane.start()
            except Exception:
                pass

    async def cleanup(application: web.Application) -> None:
        pending = application.get(_INGEST_TASKS_KEY)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if isinstance(expert_plane, SubprocessExpertPlane):
            await expert_plane.close()
        session = application.get(_SESSION_KEY)
        if session is not None:
            await session.close()
        await recorder.close()

    async def proxy(request: web.Request) -> web.StreamResponse:
        provider = request.match_info["provider"]
        upstream = normalized.get(provider)
        if upstream is None:
            raise web.HTTPNotFound(text=f"unknown upstream: {provider}")

        tail = request.match_info.get("tail", "")
        upstream_url = f"{upstream}/{tail}"
        if request.query_string:
            upstream_url = f"{upstream_url}?{request.query_string}"

        await _enforce_expert_policy(
            expert_plane,
            require_expert_plane=require_expert_plane,
        )

        session = request.app[_SESSION_KEY]
        attribution = dict(trusted_attribution or {})
        trusted_token = os.environ.get(trusted_attribution_token_env, "") if trusted_attribution_token_env else ""
        supplied_token = request.headers.get("X-LLM-Log-Service-Token", "")
        if trusted_token and hmac.compare_digest(trusted_token.encode(), supplied_token.encode()):
            for header, key in (("Worker", "worker"), ("Agent", "agent"), ("Session", "session"),
                                ("Task", "task"), ("Correlation-Id", "correlation_id"),
                                ("Causation-Id", "causation_id"), ("Plan", "plan")):
                value = request.headers.get("X-LLM-Log-" + header)
                if value and key not in attribution:
                    attribution[key] = value
            attribution["trust"] = "authenticated_service"
        elif attribution:
            attribution["trust"] = "server_configured"
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(
                request,
                expert_plane=expert_plane,
                provider=provider,
                upstream=upstream,
                upstream_url=upstream_url,
                tail=tail,
                recorder=recorder,
                classifier=classifier,
                session=session,
                attribution=attribution,
            )

        request_body = await request.read()
        started_at = _now()
        started = time.perf_counter()
        classify_task = asyncio.create_task(_classify(classifier, request_body))
        response_body = bytearray()
        response_status = 502
        response_headers: Mapping[str, str] = {}

        attempts = []
        final_provider = provider
        final_upstream = upstream
        downstream = None
        model = None
        try:
            parsed = json.loads(request_body)
            model = parsed.get("model") if isinstance(parsed, dict) else None
        except (ValueError, UnicodeError):
            pass
        scope = account_scope(request.headers)
        via = ", ".join(value for name, value in request.headers.items() if name.lower() == "via")
        fallback_loop = FALLBACK_VIA_MARKER in via.lower()
        fallback = None
        ineligible_reason = None
        if fallback_policy is not None and normalized.get(fallback_policy.target_provider) and not request.query_string and not fallback_loop:
            try:
                fallback = fallback_policy.request(provider=provider, method=request.method,
                                                   path="/" + tail, body=request_body, account=scope, check_budget=False)
            except ProviderPolicyConflict:
                ineligible_reason = "provider_policy_conflict"
        using_fallback = bool(fallback and isinstance(model, str) and
                              fallback_policy.circuit_open(scope, provider, model))
        if using_fallback:
            fallback = fallback_policy.request(provider=provider, method=request.method,
                                               path="/" + tail, body=request_body, account=scope)
            using_fallback = fallback is not None
        attempt_url = upstream_url
        attempt_headers = _request_headers(request.headers)
        attempt_body = request_body
        redaction_secrets = set()
        attempt_model = model
        retry_reason = "circuit_open" if using_fallback else ineligible_reason
        try:
            for attempt_index in range(2):
                if using_fallback:
                    final_provider = fallback_policy.target_provider
                    final_upstream = normalized[final_provider]
                    attempt_url = final_upstream + "/" + fallback_policy.target_path.lstrip("/")
                    attempt_body, attempt_headers, attempt_model = fallback
                    attempt_headers = {**attempt_headers, "Via": ", ".join(filter(None, (via, FALLBACK_VIA_MARKER)))}
                attempt_started = _now()
                redaction_secrets.update(credential_values(attempt_headers))
                async with session.request(request.method, attempt_url, headers=attempt_headers,
                                           data=attempt_body, allow_redirects=False) as upstream_response:
                    response_status = upstream_response.status
                    response_headers = upstream_response.headers
                    # Only bounded error bodies are inspected, before downstream headers exist.
                    buffered = b""
                    if response_status in (402, 429) and not using_fallback and fallback:
                        pieces = bytearray()
                        while len(pieces) <= 65536 and not upstream_response.content.at_eof():
                            chunk = await upstream_response.content.read(65537 - len(pieces))
                            if not chunk:
                                break
                            pieces.extend(chunk)
                            response_body.extend(chunk)
                        buffered = bytes(pieces)
                        reason = (classify_exhaustion(response_status, buffered)
                                  if len(buffered) <= 65536 and upstream_response.content.at_eof() else None)
                        if reason is not None:
                            fallback = fallback_policy.request(provider=provider, method=request.method,
                                                               path="/" + tail, body=request_body, account=scope)
                        if reason is not None and fallback is not None:
                            attempts.append(_attempt_record(number=len(attempts) + 1, provider=provider,
                                model=model, status=response_status, reason=reason.value, body=buffered,
                                headers=response_headers, started_at=attempt_started, committed=False,
                                terminal_class="exhaustion", policy=fallback_policy))
                            fallback_policy.mark_exhausted(scope, provider, model)
                            using_fallback = True
                            retry_reason = reason.value
                            response_body.clear()
                            continue
                    downstream = web.StreamResponse(status=response_status, reason=upstream_response.reason,
                                                    headers=_response_headers(upstream_response.headers))
                    await downstream.prepare(request)
                    if buffered:
                        await downstream.write(buffered)
                    async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                        response_body.extend(chunk)
                        await downstream.write(chunk)
                    await downstream.write_eof()
                    attempts.append(_attempt_record(number=len(attempts) + 1, provider=final_provider,
                        model=attempt_model, status=response_status, reason=retry_reason, body=bytes(response_body),
                        headers=response_headers, started_at=attempt_started, committed=True,
                        terminal_class="success" if response_status < 400 else "upstream_error", policy=fallback_policy))
                    break
        except Exception:
            # A prepared response can only be terminated; replay would duplicate output.
            committed = downstream is not None and downstream.prepared
            attempts.append(_attempt_record(number=len(attempts) + 1, provider=final_provider,
                model=attempt_model, status=response_status if committed else 502,
                reason="midstream_failure" if committed else "transport_error", body=bytes(response_body),
                headers=response_headers, started_at=attempt_started, committed=committed,
                terminal_class="midstream_failure" if committed else "transport_failure", policy=fallback_policy))
            if downstream is not None and downstream.prepared:
                if request.transport is not None:
                    request.transport.close()
            else:
                response_status = 502
                response_headers = {}
                response_body = bytearray(b"upstream request failed")
                downstream = web.Response(status=502, text="upstream request failed")

        intents = await classify_task
        completed_at = _now()
        latency_ms = round((time.perf_counter() - started) * 1000)
        event = CaptureEvent.from_bytes(
            event_id=str(uuid.uuid4()),
            provider=provider,
            upstream=final_upstream,
            method=request.method,
            path="/" + tail,
            query=request.query_string,
            request_headers=request.headers,
            request_body=request_body,
            response_status=response_status,
            response_headers=response_headers,
            response_body=bytes(response_body),
            started_at=started_at,
            completed_at=completed_at,
            latency_ms=latency_ms,
            intents=intents,
            attribution=attribution,
            attempts=attempts,
            final_provider=final_provider,
            redaction_secrets=redaction_secrets,
        )
        await recorder.record(event)
        _schedule_expert_ingest(request.app, expert_plane, event, request_body)
        return downstream

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    install_analytics_routes(app, recorder.root / "events.jsonl")
    app.router.add_route("*", "/{provider}/{tail:.*}", proxy)
    return app
