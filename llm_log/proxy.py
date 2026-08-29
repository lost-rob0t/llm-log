from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Mapping, Protocol

from aiohttp import ClientSession, ClientTimeout, web

from .recorder import CaptureEvent, RecorderActor

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
_SESSION_KEY = web.AppKey("session", ClientSession)


class Classifier(Protocol):
    def classify(self, text: str) -> list[str]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _REQUEST_DROP}


def _response_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in headers.items() if name.lower() not in _RESPONSE_DROP]


async def _classify(classifier: Classifier | None, request_body: bytes) -> list[str]:
    if classifier is None:
        return []
    text = request_body.decode("utf-8", errors="replace")
    try:
        return await asyncio.to_thread(classifier.classify, text)
    except Exception:
        return []


def build_app(
    upstreams: Mapping[str, str],
    recorder: RecorderActor,
    classifier: Classifier | None,
    *,
    timeout_seconds: float = 600.0,
) -> web.Application:
    normalized = {name: url.rstrip("/") for name, url in upstreams.items()}
    app = web.Application(client_max_size=1024**3)

    async def startup(application: web.Application) -> None:
        await recorder.start()
        application[_SESSION_KEY] = ClientSession(
            timeout=ClientTimeout(total=timeout_seconds),
            auto_decompress=False,
        )

    async def cleanup(application: web.Application) -> None:
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

        request_body = await request.read()
        started_at = _now()
        started = time.perf_counter()
        classify_task = asyncio.create_task(_classify(classifier, request_body))
        session = request.app[_SESSION_KEY]
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
        return downstream

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    app.router.add_route("*", "/{provider}/{tail:.*}", proxy)
    return app
