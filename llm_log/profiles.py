from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


OPENCODE_OPENROUTER_SOURCE_BLOB = "0f295fb0950f1a5ddc6ed1a08af1e9d38b0dea54"

_FORBIDDEN_PROFILE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "upgrade",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}

_METADATA_HEADERS = {
    "http-referer",
    "x-title",
    "x-source",
    "x-client-name",
    "x-client-version",
    "x-openai-client-user-agent",
}
_METADATA_PREFIXES = (
    "x-stainless-",
    "x-sdk-",
)


@dataclass(frozen=True, slots=True)
class OutboundProfile:
    name: str
    version: str
    set_headers: Mapping[str, str]
    drop_headers: frozenset[str] = frozenset()
    drop_prefixes: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.name}@{self.version}"


_PROFILES = {
    "transparent": OutboundProfile(
        name="transparent",
        version="v1",
        set_headers={},
    ),
    "metadata-minimizing-v1": OutboundProfile(
        name="metadata-minimizing",
        version="v1",
        set_headers={},
        drop_headers=frozenset(_METADATA_HEADERS | {"user-agent"}),
        drop_prefixes=_METADATA_PREFIXES,
    ),
    "opencode-openrouter-v1": OutboundProfile(
        name="opencode-openrouter",
        # Source-verified against anomalyco/opencode dev:
        # packages/core/src/plugin/provider/openrouter.ts blob above.
        version=f"blob-{OPENCODE_OPENROUTER_SOURCE_BLOB[:12]}",
        set_headers={
            "HTTP-Referer": "https://opencode.ai/",
            "X-Title": "opencode",
        },
        drop_headers=frozenset({"http-referer", "x-title"}),
    ),
}


def profile_names() -> tuple[str, ...]:
    return tuple(sorted(_PROFILES))


def resolve_profile(name: str) -> OutboundProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(profile_names())
        raise ValueError(f"unknown outbound profile {name!r}; choose one of: {choices}") from exc


def apply_profile(
    headers: Mapping[str, str],
    profile_name: str | None,
) -> tuple[dict[str, str], str | None]:
    if profile_name is None:
        return dict(headers), None

    profile = resolve_profile(profile_name)
    result: dict[str, str] = {}

    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _FORBIDDEN_PROFILE_HEADERS:
            # Preserve framing/authentication exactly as supplied by the
            # already-sanitized transport layer. Profiles never own them.
            result[name] = value
            continue
        if lowered in profile.drop_headers:
            continue
        if any(lowered.startswith(prefix) for prefix in profile.drop_prefixes):
            continue
        result[name] = value

    for name, value in profile.set_headers.items():
        lowered = name.lower()
        if lowered in _FORBIDDEN_PROFILE_HEADERS:
            raise ValueError(f"profile {profile.identity} cannot set protected header {name}")
        for existing in tuple(result):
            if existing.lower() == lowered:
                del result[existing]
        result[name] = value

    return result, profile.identity
