from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aiohttp import web

from .quotas import install_quota_routes

_BUCKET_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=f"invalid {name} timestamp") from exc
    if parsed.tzinfo is None:
        raise web.HTTPBadRequest(text=f"{name} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _filters(request: web.Request) -> tuple[datetime | None, datetime | None, str | None, str | None]:
    start = _timestamp(request.query["start"], "start") if "start" in request.query else None
    end = _timestamp(request.query["end"], "end") if "end" in request.query else None
    if start is not None and end is not None and start >= end:
        raise web.HTTPBadRequest(text="start must be before end")
    provider = request.query.get("provider") or None
    model = request.query.get("model") or None
    return start, end, provider, model


def _events(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except FileNotFoundError:
        return


def _selected(request: web.Request, path: Path) -> list[tuple[dict[str, Any], datetime]]:
    start, end, provider, model = _filters(request)
    selected = []
    for event in _events(path):
        try:
            completed = _timestamp(event["completed_at"], "captured")
        except (KeyError, web.HTTPBadRequest):
            continue
        if start is not None and completed < start:
            continue
        if end is not None and completed >= end:
            continue
        if provider is not None and event.get("provider") != provider:
            continue
        if model is not None and event.get("model") != model:
            continue
        selected.append((event, completed))
    return selected


def _tokens(event: dict[str, Any]) -> tuple[int | None, int | None]:
    incoming = event.get("input_tokens")
    outgoing = event.get("output_tokens")
    if not isinstance(incoming, int) or isinstance(incoming, bool) or incoming < 0:
        incoming = None
    if not isinstance(outgoing, int) or isinstance(outgoing, bool) or outgoing < 0:
        outgoing = None
    return incoming, outgoing


def _totals(rows: Iterable[tuple[dict[str, Any], datetime]]) -> dict[str, int]:
    request_count = usage_count = incoming = outgoing = 0
    for event, _ in rows:
        request_count += 1
        event_input, event_output = _tokens(event)
        if event_input is not None or event_output is not None:
            usage_count += 1
        incoming += event_input or 0
        outgoing += event_output or 0
    return {
        "request_count": request_count,
        "requests_with_usage": usage_count,
        "input_tokens": incoming,
        "output_tokens": outgoing,
        "total_tokens": incoming + outgoing,
    }


async def summary(request: web.Request) -> web.Response:
    return web.json_response(_totals(_selected(request, request.app[EVENTS_PATH_KEY])))


async def models(request: web.Request) -> web.Response:
    grouped: dict[tuple[str, str], list[tuple[dict[str, Any], datetime]]] = defaultdict(list)
    for event, completed in _selected(request, request.app[EVENTS_PATH_KEY]):
        grouped[(str(event.get("provider") or "unknown"), str(event.get("model") or "unknown"))].append((event, completed))
    entries = [
        {"provider": provider, "model": model, **_totals(rows)}
        for (provider, model), rows in sorted(grouped.items())
    ]
    return web.json_response({"models": entries})


async def timeline(request: web.Request) -> web.Response:
    granularity = request.query.get("granularity", "minute")
    if granularity not in _BUCKET_SECONDS:
        raise web.HTTPBadRequest(text="granularity must be minute, hour, or day")
    seconds = _BUCKET_SECONDS[granularity]
    grouped: dict[int, list[tuple[dict[str, Any], datetime]]] = defaultdict(list)
    selected = _selected(request, request.app[EVENTS_PATH_KEY])
    for event, completed in selected:
        epoch = int(completed.timestamp())
        grouped[epoch - epoch % seconds].append((event, completed))
    buckets = [
        {
            "start": datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z"),
            "bucket_seconds": seconds,
            **_totals(rows),
        }
        for epoch, rows in sorted(grouped.items())
    ]
    return web.json_response({"granularity": granularity, "buckets": buckets})


EVENTS_PATH_KEY = web.AppKey("analytics-events-path", Path)


def openapi_document() -> dict[str, Any]:
    query_parameters = [
        {"name": name, "in": "query", "required": False, "schema": schema}
        for name, schema in (
            ("start", {"type": "string", "format": "date-time"}),
            ("end", {"type": "string", "format": "date-time"}),
            ("provider", {"type": "string"}),
            ("model", {"type": "string"}),
        )
    ]
    response = {"200": {"description": "Token analytics"}, "400": {"description": "Invalid query"}}
    paths = {
        "/api/v1/stats/summary": {"get": {"summary": "Aggregate token I/O totals", "parameters": query_parameters, "responses": response}},
        "/api/v1/stats/models": {"get": {"summary": "Token totals grouped by provider and model", "parameters": query_parameters, "responses": response}},
        "/api/v1/stats/timeline": {"get": {"summary": "Bucketed token I/O timeline", "parameters": [*query_parameters, {"name": "granularity", "in": "query", "schema": {"type": "string", "enum": list(_BUCKET_SECONDS), "default": "minute"}}], "responses": response}},
    }
    paths["/api/v1/quotas"] = {"get": {"summary": "Cached provider-reported subscription quotas (localhost only)", "responses": {"200": {"description": "Version 1 quota snapshot; percentages are used, not remaining"}, "403": {"description": "Local clients only"}}}}
    return {"openapi": "3.1.0", "info": {"title": "llm-log analytics API", "version": "1.0.0"}, "paths": paths}


async def openapi(_request: web.Request) -> web.Response:
    return web.json_response(openapi_document())


async def docs(_request: web.Request) -> web.Response:
    return web.Response(
        content_type="text/html",
        text='''<!doctype html><html><head><title>llm-log API</title><link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"></head><body><div id="swagger-ui"></div><script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script><script>SwaggerUIBundle({url:"/openapi.json",dom_id:"#swagger-ui"})</script></body></html>''',
    )


def install_analytics_routes(app: web.Application, events_path: Path) -> None:
    app[EVENTS_PATH_KEY] = events_path
    app.router.add_get("/api/v1/stats/summary", summary)
    app.router.add_get("/api/v1/stats/timeline", timeline)
    app.router.add_get("/api/v1/stats/models", models)
    app.router.add_get("/openapi.json", openapi)
    app.router.add_get("/docs", docs)
    install_quota_routes(app)
