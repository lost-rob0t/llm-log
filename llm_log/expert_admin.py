from __future__ import annotations

import ipaddress
from typing import Any

from aiohttp import web

from .expert_adapter import ExpertAdapterError, READ_ONLY_EXPERT_OPERATIONS
from .expert_capture import replay_capture_event


_ADMIN_RUNNER_KEY = web.AppKey("llm-log-expert-admin-runner", web.AppRunner)


def _loopback_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("expert admin listener must use a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("expert admin listener must remain loopback-only")
    return value


async def _json_object(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="request body must be JSON") from exc
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return value


def _json_error(status: int, code: str, message: str) -> web.Response:
    return web.json_response(
        {"status": "error", "error": {"code": code, "message": message}},
        status=status,
    )


def build_expert_admin_app(expert_plane: Any) -> web.Application:
    app = web.Application(client_max_size=1024**3)

    async def health(_request: web.Request) -> web.Response:
        if expert_plane is None:
            return _json_error(503, "expert_unavailable", "expert plane is not configured")
        try:
            result = await expert_plane.health()
        except ExpertAdapterError as exc:
            return _json_error(503, "expert_unavailable", str(exc))
        return web.json_response({"status": "ok", "result": result})

    async def query(request: web.Request) -> web.Response:
        if expert_plane is None:
            return _json_error(503, "expert_unavailable", "expert plane is not configured")
        body = await _json_object(request)
        operation = body.get("operation")
        payload = body.get("payload", {})
        if operation not in READ_ONLY_EXPERT_OPERATIONS:
            return _json_error(400, "operation_not_read_only", "operation is not an allowed expert query")
        if not isinstance(payload, dict):
            return _json_error(400, "invalid_payload", "payload must be an object")
        try:
            result = await expert_plane.query(operation, payload)
        except (ExpertAdapterError, ValueError) as exc:
            return _json_error(400, "expert_query_error", str(exc))
        return web.json_response({"status": "ok", "result": result})

    async def backfill_event(request: web.Request) -> web.Response:
        if expert_plane is None:
            return _json_error(503, "expert_unavailable", "expert plane is not configured")
        body = await _json_object(request)
        event = body.get("event")
        record_transport = body.get("record_transport_evidence", False)
        if not isinstance(event, dict):
            return _json_error(400, "invalid_event", "event must be an object")
        if not isinstance(record_transport, bool):
            return _json_error(400, "invalid_option", "record_transport_evidence must be boolean")
        try:
            result = await replay_capture_event(
                expert_plane,
                event,
                record_transport_evidence=record_transport,
            )
        except ValueError as exc:
            return _json_error(400, "invalid_event", str(exc))
        except ExpertAdapterError as exc:
            return _json_error(409, "expert_backfill_error", str(exc))
        return web.json_response({"status": "ok", "result": result})

    app.router.add_get("/health", health)
    app.router.add_post("/query", query)
    app.router.add_post("/backfill-event", backfill_event)
    return app


def install_expert_admin_listener(
    app: web.Application,
    expert_plane: Any,
    *,
    listen: str = "127.0.0.1",
    port: int = 8788,
) -> None:
    if port == 0:
        return
    if not 1 <= port <= 65535:
        raise ValueError("expert admin port must be 0 or 1..65535")
    listen = _loopback_address(listen)

    async def startup(parent: web.Application) -> None:
        admin = build_expert_admin_app(expert_plane)
        runner = web.AppRunner(admin)
        await runner.setup()
        site = web.TCPSite(runner, listen, port)
        await site.start()
        parent[_ADMIN_RUNNER_KEY] = runner

    async def cleanup(parent: web.Application) -> None:
        runner = parent.get(_ADMIN_RUNNER_KEY)
        if runner is not None:
            await runner.cleanup()

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
