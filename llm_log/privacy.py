"""Redaction for persisted copies only; never apply these helpers to forwarding.

Body hashes describe redacted stored bytes, not credential-bearing wire bytes.
Opaque binary payloads retain their encoding after scanning their byte-preserving
Latin-1 representation for recognizable credential syntax.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Mapping
from urllib.parse import unquote, unquote_plus

REDACTED = "<redacted>"
_SECRET_NAMES = {
    "authorization", "proxyauthorization", "cookie", "setcookie", "apikey",
    "xapikey", "openaiapikey", "anthropicapikey", "accesskey", "secretkey",
    "accesskeyid", "secretaccesskey", "password", "passwd", "clientsecret",
    "token", "accesstoken", "refreshtoken", "idtoken", "authtoken", "access", "refresh",
    "authentication", "xllmlogservicetoken", "credential", "credentials", "authorizationcode",
}
_BEARER = re.compile(r"(?ai)\b(Bearer|Basic)(?:\s+|%20|%3A)[A-Za-z0-9._~+/=:%-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", re.ASCII)
_KEY = re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\b", re.ASCII)
_ASSIGNMENT = re.compile(
    r"(?i)([\w%.-]*(?:token|api[_-]?key|password|client[_-]?secret|authorization|credential|access|refresh)[\w%.-]*)(\s*[=:]\s*)([^\s&,;\"'<>}]+)"
)
_QUOTED_SECRET = re.compile(r'''(?i)(["'])([\w.-]+)\1(\s*:\s*)(["'])([^"'\r\n]*)(?:["']|$)''')
_USERINFO = re.compile(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", re.I)


def secret_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", unquote(name).lower())
    return normalized in _SECRET_NAMES or normalized.endswith(
        ("token", "apikey", "clientsecret", "authorization", "cookie")
    )


def _patterns(text: str) -> str:
    text = _USERINFO.sub(r"\1" + REDACTED + "@", text)
    text = _BEARER.sub(lambda match: match[1] + " " + REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _KEY.sub(REDACTED, text)
    text = _QUOTED_SECRET.sub(
        lambda match: match[1] + match[2] + match[1] + match[3] + match[4] + REDACTED + match[4]
        if secret_name(match[2]) else match[0], text
    )
    return _ASSIGNMENT.sub(
        lambda match: match[1] + match[2] + REDACTED if secret_name(match[1]) else match[0], text
    )


def redact_text(text: str) -> str:
    """Redact JSON, NDJSON, SSE, URLs and recognizable credentials in prose."""
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        safe = redact_value(parsed)
        return text if safe == parsed else json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        prefix = "data:" if stripped.startswith("data:") else ""
        candidate = stripped[5:].strip() if prefix else stripped
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, (dict, list)):
            safe = redact_value(value)
            if safe != value:
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                line = prefix + (" " if prefix else "") + json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + ending
        lines.append(_patterns(line))
    return "".join(lines)


def redact_bytes(raw: bytes) -> bytes:
    try:
        return redact_text(raw.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        return redact_text(raw.decode("latin-1")).encode("latin-1", errors="replace")


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        result = {str(key): REDACTED if secret_name(str(key)) else redact_value(child)
                  for key, child in value.items()}
        # WebSocket journal/capture envelopes can contain credential-bearing binary JSON.
        if value.get("encoding") == "base64" and isinstance(value.get("data"), str):
            try:
                raw = base64.b64decode(value["data"], validate=True)
                result["data"] = base64.b64encode(redact_bytes(raw)).decode("ascii")
            except (ValueError, TypeError):
                result["data"] = REDACTED
        return result
    if isinstance(value, (list, tuple)):
        return [redact_value(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: REDACTED if secret_name(name) else redact_text(value)
            for name, value in headers.items()}


def bounded_identifier(value: Any, limit: int = 256) -> str | None:
    """Accept bounded, printable non-secret attribution/metadata identifiers."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value if redact_text(value) == value and REDACTED not in value else None


def credential_values(value: Any) -> set[str]:
    """Collect explicitly labeled credentials to remove echoes in error prose."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if secret_name(str(key)) and isinstance(child, str) and child and child != REDACTED:
                found.add(child)
                if child.lower().startswith(('bearer ', 'basic ')):
                    found.add(child.split(None, 1)[1])
            else:
                found.update(credential_values(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(credential_values(child))
    elif isinstance(value, (str, bytes)):
        text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            found.update(credential_values(parsed))
        elif isinstance(parsed, str) and parsed != text:
            found.update(credential_values(parsed))
        else:
            for line in text.splitlines():
                candidate = line[5:].strip() if line.startswith('data:') else line.strip()
                if candidate == text:
                    continue
                found.update(credential_values(candidate))
        for match in _ASSIGNMENT.finditer(text):
            if secret_name(match[1]) and match[3] != REDACTED:
                found.add(match[3])
    # Keep exact wire values and bounded decoded forms. Two rounds cover a
    # percent-encoded credential nested in a URL parameter without unbounded
    # decoding or accepting unrelated free text as a credential.
    for _ in range(2):
        for secret in tuple(found):
            if len(secret) <= 8192:
                for decode in (unquote, unquote_plus):
                    decoded = decode(secret)
                    if decoded and decoded != REDACTED:
                        found.add(decoded)
    return found


def redact_known(value: Any, secrets: set[str]) -> Any:
    """Remove credential echoes from decoded JSON/SSE copies and plain text.

    Unchanged strings retain their original formatting. Parsing JSON string
    values before replacement also removes Unicode-escaped credential echoes.
    Forwarding callers must continue to use the original bytes.
    """
    secrets = {secret for secret in secrets if secret and secret != REDACTED}
    if not secrets:
        return value

    def replace_text(text: str) -> str:
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            # Preserve opaque binary bytes while removing exact credential
            # bytes; base64 frame envelopes are decoded separately below.
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret.encode(), REDACTED.encode())
            return value
        return redact_known(text, secrets).encode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            safe = redact_known(parsed, secrets)
            return (value if safe == parsed else
                    json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
        if isinstance(parsed, str):
            safe = redact_known(parsed, secrets)
            return value if safe == parsed else json.dumps(safe, ensure_ascii=False)
        lines = []
        for line in value.splitlines(keepends=True):
            stripped = line.strip()
            prefix = "data:" if stripped.startswith("data:") else ""
            candidate = stripped[5:].strip() if prefix else stripped
            try:
                document = json.loads(candidate)
            except (ValueError, RecursionError):
                document = None
            if isinstance(document, (dict, list)):
                safe = redact_known(document, secrets)
                if safe != document:
                    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                    line = prefix + (" " if prefix else "") + json.dumps(
                        safe, ensure_ascii=False, separators=(",", ":")) + ending
            lines.append(replace_text(line))
        return "".join(lines)
    if isinstance(value, Mapping):
        result = {key: redact_known(child, secrets) for key, child in value.items()}
        if value.get("encoding") == "base64" and isinstance(value.get("data"), str):
            try:
                raw = base64.b64decode(value["data"], validate=True)
                result["data"] = base64.b64encode(redact_known(raw, secrets)).decode("ascii")
            except (ValueError, TypeError):
                result["data"] = REDACTED
        return result
    if isinstance(value, (list, tuple)):
        return [redact_known(child, secrets) for child in value]
    return value
