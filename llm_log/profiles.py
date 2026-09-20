from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


OPENCODE_OPENROUTER_SOURCE_BLOB = "0f295fb0950f1a5ddc6ed1a08af1e9d38b0dea54"
OPENCODE_REQUEST_SOURCE_BLOB = "e000d6ca49b5f9792cc6ddc30ef25e523a33e9c4"
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")

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
    opencode_user_agent: bool = False

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
    "opencode-client-v1": OutboundProfile(
        name="opencode-client",
        # OpenCode's LLM request path defines USER_AGENT as
        # opencode/<InstallationVersion>; source blob pinned above.
        version=f"blob-{OPENCODE_REQUEST_SOURCE_BLOB[:12]}",
        set_headers={},
        drop_headers=frozenset({"user-agent"}),
        opencode_user_agent=True,
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
        drop_headers=frozenset({"http-referer", "x-title", "user-agent"}),
        opencode_user_agent=True,
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
    *,
    opencode_version: str | None = None,
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

    identity = profile.identity
    if profile.opencode_user_agent:
        if opencode_version is None or not _VERSION.fullmatch(opencode_version):
            raise ValueError(
                f"profile {profile.identity} requires an explicit safe OpenCode version"
            )
        for existing in tuple(result):
            if existing.lower() == "user-agent":
                del result[existing]
        result["User-Agent"] = f"opencode/{opencode_version}"
        identity = f"{identity};opencode={opencode_version}"

    return result, identity
