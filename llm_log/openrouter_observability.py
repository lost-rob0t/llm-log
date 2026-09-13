from __future__ import annotations

import json
from typing import Any

from aiohttp import web


@web.middleware
async def openrouter_metadata_middleware(request: web.Request, handler):
    if request.match_info.get("provider") != "openrouter":
        return await handler(request)

    headers = request.headers.copy()
    headers["X-OpenRouter-Metadata"] = "enabled"
    return await handler(request.clone(headers=headers))


def _json_payloads(response_body: bytes):
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


def _find_quantization(value: Any) -> str | None:
    if isinstance(value, dict):
        quantization = value.get("quantization")
        if isinstance(quantization, str) and quantization:
            return quantization
        for child in value.values():
            found = _find_quantization(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_quantization(child)
            if found is not None:
                return found
    return None


def observe_openrouter_response(provider: str, response_body: bytes) -> dict[str, Any]:
    if provider != "openrouter":
        return {}

    selected_provider: str | None = None
    router_metadata: dict[str, Any] | None = None
    quantization: str | None = None
    quantization_source = "undisclosed"

    for payload in _json_payloads(response_body):
        payload_provider = payload.get("provider")
        if isinstance(payload_provider, str) and payload_provider:
            selected_provider = payload_provider

        metadata = payload.get("openrouter_metadata")
        if isinstance(metadata, dict):
            router_metadata = metadata
            disclosed = _find_quantization(metadata)
            if disclosed is not None:
                quantization = disclosed
                quantization_source = "router_metadata"

        if quantization is None:
            disclosed = payload.get("quantization")
            if isinstance(disclosed, str) and disclosed:
                quantization = disclosed
                quantization_source = "response"

    observation: dict[str, Any] = {
        "router": "openrouter",
        "quantization": quantization or "unknown",
        "quantization_source": quantization_source,
    }
    if selected_provider is not None:
        observation["selected_provider"] = selected_provider
    if router_metadata is not None:
        observation["metadata"] = router_metadata
    return observation
