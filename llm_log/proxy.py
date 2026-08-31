from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Mapping, Protocol

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

from .expert_adapter import SubprocessExpertPlane
from .recorder import CaptureEvent, RecorderActor, WebSocketFrame

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
_REQUEST_DROP = _HOP_BY_HOP | {"host", "content-length"}
_RESPONSE_DROP = _HOP_BY_HOP
_WS_REQUEST_DROP = _REQUEST_DROP | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}
_SESSION_KEY = web.AppKey("session", ClientSession)


class Classifier(Protocol):
    def classify(self, text: str) -> list[str]: ...


class ExpertPlane(Protocol):
    async def health(self) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _REQUEST_DROP}


def _websocket_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _WS_REQUEST_DROP
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
        message = _extract_user_message(request_body)
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
) -> None:
    async for message in source:
        if message.type == WSMsgType.TEXT:
            raw = message.data.encode("utf-8")
            captured.extend(_websocket_frame(message.type, message.data))
            await recorder.journal_frame(
                WebSocketFrame.from_bytes(
                    event_id=event_id,
                    direction=direction,
                    frame_type="text",
                    payload=raw,
                    timestamp=_now(),
                )
            )
            await target.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            captured.extend(_websocket_frame(message.type, message.data))
            await recorder.journal_frame(
                WebSocketFrame.from_bytes(
                    event_id=event_id,
                    direction=direction,
                    frame_type="binary",
                    payload=message.data,
                    timestamp=_now(),
                )
            )
            await target.send_bytes(message.data)
        elif message.type == WSMsgType.ERROR:
            error = source.exception()
            if error is not None:
                raise error
            raise RuntimeError("websocket relay failed")


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
) -> web.StreamResponse:
    event_id = str(uuid.uuid4())
    started_at = _now()
    started = time.perf_counter()
    client_frames = bytearray()
    server_frames = bytearray()
    protocols = _websocket_protocols(request.headers)

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
        )
        await recorder.record(event)
        _schedule_expert_ingest(request.app, expert_plane, event, b"")
        return web.Response(status=502, text="upstream websocket connection failed")

    selected_protocol = upstream_socket.protocol
    downstream = web.WebSocketResponse(
        protocols=(selected_protocol,) if selected_protocol is not None else (),
        autoping=True,
    )

    try:
        await downstream.prepare(request)
        client_to_upstream = asyncio.create_task(
            _relay_websocket(
                downstream,
                upstream_socket,
                client_frames,
                recorder=recorder,
                event_id=event_id,
                direction="client_to_upstream",
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
            ),
            name="llm-log-ws-upstream-to-client",
        )
        tasks = {client_to_upstream, upstream_to_client}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        await upstream_socket.close()
        await downstream.close()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if not upstream_socket.closed:
            await upstream_socket.close()
        if not downstream.closed:
            await downstream.close()

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
        response_status=101,
        response_headers=response_headers,
        response_body=response_body,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=round((time.perf_counter() - started) * 1000),
        intents=intents,
        transport="websocket",
    )
    await recorder.record(event)
    _schedule_expert_ingest(request.app, expert_plane, event, bytes(client_frames))
    return downstream


def build_app(
    upstreams: Mapping[str, str],
    recorder: RecorderActor,
    classifier: Classifier | None,
    *,
    timeout_seconds: float = 600.0,
    expert_plane: ExpertPlane | None = None,
    require_expert_plane: bool = False,
) -> web.Application:
    normalized = {name: url.rstrip("/") for name, url in upstreams.items()}
    app = web.Application(client_max_size=1024**3)

    async def startup(application: web.Application) -> None:
        await recorder.start()
        application[_SESSION_KEY] = ClientSession(
            timeout=ClientTimeout(total=timeout_seconds),
            auto_decompress=False,
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
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(
                request,
                provider=provider,
                upstream=upstream,
                upstream_url=upstream_url,
                tail=tail,
                recorder=recorder,
                classifier=classifier,
                session=session,
            )

        request_body = await request.read()
        started_at = _now()
        started = time.perf_counter()
        classify_task = asyncio.create_task(_classify(classifier, request_body))
        response_body = bytearray()
        response_status = 502
        response_headers: Mapping[str, str] = {}

        try:
            async with session.request(
                request.method,
                upstream_url,
                headers=_request_headers(request.headers),
                data=request_body,
                allow_redirects=False,
            ) as upstream_response:
                response_status = upstream_response.status
                response_headers = upstream_response.headers
                downstream = web.StreamResponse(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=_response_headers(upstream_response.headers),
                )
                await downstream.prepare(request)
                async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                    response_body.extend(chunk)
                    await downstream.write(chunk)
                await downstream.write_eof()
        except Exception as exc:
            if not response_body:
                response_body.extend(str(exc).encode("utf-8", errors="replace"))
            if not request.protocol.transport or request.protocol.transport.is_closing():
                raise
            downstream = web.Response(status=502, text="upstream request failed")

        intents = await classify_task
        completed_at = _now()
        latency_ms = round((time.perf_counter() - started) * 1000)
        event = CaptureEvent.from_bytes(
            event_id=str(uuid.uuid4()),
            provider=provider,
            upstream=upstream,
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
        )
        await recorder.record(event)
        _schedule_expert_ingest(request.app, expert_plane, event, request_body)
        return downstream

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_route("*", "/{provider}/{tail:.*}", proxy)
    return app
