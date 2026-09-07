from __future__ import annotations

import json
from collections.abc import Sequence

from aiohttp import web

_OPENROUTER_INFERENCE_PATHS = (
    "api/v1/chat/completions",
    "api/v1/completions",
    "api/v1/responses",
    "api/v1/messages",
    "api/v1/cursor",
)


class QuantizationPolicyError(ValueError):
    pass


def apply_quantization_policy(
    request_body: bytes,
    allowed_quantizations: Sequence[str],
) -> bytes:
    allowed = tuple(allowed_quantizations)
    if not allowed:
        raise QuantizationPolicyError("active quantization preset is empty")

    try:
        payload = json.loads(request_body)
    except json.JSONDecodeError as exc:
        raise QuantizationPolicyError("OpenRouter inference request must be valid JSON") from exc

    if not isinstance(payload, dict):
        raise QuantizationPolicyError("OpenRouter inference request must be a JSON object")

    provider = payload.get("provider")
    if provider is None:
        provider = {}
    elif not isinstance(provider, dict):
        raise QuantizationPolicyError("provider must be an object")
    else:
        provider = dict(provider)

    requested = provider.get("quantizations")
    if requested is None:
        effective = list(allowed)
    else:
        if not isinstance(requested, list) or not all(
            isinstance(value, str) for value in requested
        ):
            raise QuantizationPolicyError("provider.quantizations must be an array of strings")
        effective = [value for value in requested if value in allowed]
        if not effective:
            raise QuantizationPolicyError(
                "no quantization remains after applying the active llm-log preset"
            )

    provider["quantizations"] = effective
    payload["provider"] = provider
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_openrouter_inference_request(request: web.Request) -> bool:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return False
    if request.match_info.get("provider") != "openrouter":
        return False
    tail = request.match_info.get("tail", "").lstrip("/")
    return any(tail == path or tail.startswith(path + "/") for path in _OPENROUTER_INFERENCE_PATHS)


def quantization_policy_middleware(
    allowed_quantizations: Sequence[str],
):
    allowed = tuple(allowed_quantizations)

    @web.middleware
    async def middleware(request: web.Request, handler):
        if not _is_openrouter_inference_request(request):
            return await handler(request)

        request_body = await request.read()
        try:
            rewritten = apply_quantization_policy(request_body, allowed)
        except QuantizationPolicyError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

        # aiohttp caches Request.read() in _read_bytes. Replacing that cache lets the
        # existing transparent proxy forward the policy-constrained JSON body without
        # duplicating the proxy implementation or touching streaming response handling.
        request._read_bytes = rewritten
        return await handler(request)

    return middleware
