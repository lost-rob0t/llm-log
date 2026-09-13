"""Bounded, provider-reported quota observations; never token-based estimates."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

MAX_WINDOWS = 64
MAX_DURATION = 366 * 86400
_ID = re.compile(r"[A-Za-z0-9_+. /:-]{1,80}\Z")


def number(value: Any, low: float = 0, high: float = 1e15) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high or not math.isfinite(value):
        return None
    return value


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _window(meter: str, slot: str, percent: Any, seconds: Any, reset: Any) -> dict:
    percent = number(percent, high=100)
    return {
        "id": f"{meter}:{slot}", "meter": meter,
        "used_percent": percent,
        "duration_seconds": number(seconds, low=1, high=MAX_DURATION),
        "resets_at": number(reset, low=1, high=253402300799),
        "state": "known" if percent is not None else "unknown",
    }


def _provider(label: str, scope: str, source: str, plan: Any, now: float, windows: list) -> dict:
    return {"label": label, "scope": scope, "source": source,
            "plan": text(plan), "observed_at": now, "windows": windows,
            "status": "ok" if windows else "unavailable"}


def normalize_codex(raw: dict, account: dict, now: float) -> dict:
    """Normalize account/rateLimits/read; its quota scope is Codex, not ChatGPT chat."""
    raw = _object(raw)
    legacy = _object(raw.get("rateLimits"))
    plan = _object(_object(account).get("account")).get("planType") or legacy.get("planType")
    buckets = _object(raw.get("rateLimitsByLimitId"))
    if not buckets:
        buckets = {text(legacy.get("limitId")) or "codex": legacy}
    if len(buckets) > MAX_WINDOWS // 2:
        raise ValueError("too many quota meters")
    windows = []
    for key, bucket in sorted(buckets.items()):
        meter = text(key)
        if meter is None or not isinstance(bucket, dict):
            raise ValueError("invalid quota meter")
        plan = plan or bucket.get("planType")
        for slot in ("primary", "secondary"):
            data = bucket.get(slot)
            if data is None:
                continue
            if not isinstance(data, dict):
                raise ValueError("invalid quota window")
            minutes = number(data.get("windowDurationMins"), low=1, high=MAX_DURATION / 60)
            windows.append(_window(meter, slot, data.get("usedPercent"),
                                   minutes * 60 if minutes is not None else None,
                                   data.get("resetsAt")))
    # credits.unlimited describes credit purchasing, NOT an unlimited subscription.
    return _provider("GPT", "codex", "codex-app-server", plan, now, windows)


def normalize_zai(raw: dict, now: float) -> dict:
    """Normalize Z.AI's quota/limit response (numeric unit codes retained in IDs).

    Endpoint/auth: official zai-coding-plugins, commit 0446d0b.
    Credit windows/unit codes: CodexBar zai.js, commit d394565.
    Unit 1=day, 3=hour, 5=minute, 6=week. Unknown units stay unknown.
    """
    raw = _object(raw)
    if raw.get("success") is not True or raw.get("code", 200) != 200:
        raise ValueError("provider rejected quota request")
    data = _object(raw.get("data"))
    limits = data.get("limits")
    if not isinstance(limits, list) or len(limits) > MAX_WINDOWS:
        raise ValueError("invalid quota limits")
    windows = []
    multipliers = {1: 86400, 3: 3600, 5: 60, 6: 604800}
    for entry in limits:
        if not isinstance(entry, dict):
            raise ValueError("invalid quota entry")
        kind = entry.get("type")
        # MCP monthly accounting is a separate product, not Coding Plan usage.
        if kind not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            continue
        unit, count = entry.get("unit"), number(entry.get("number"), low=1, high=MAX_DURATION)
        multiplier = multipliers.get(unit) if type(unit) is int else None
        seconds = count * multiplier if count is not None and multiplier else None
        reset_ms = number(entry.get("nextResetTime"), low=1e12, high=253402300799000)
        reset = reset_ms / 1000 if reset_ms is not None else None
        if seconds == 18000 and reset is not None and reset > now + 18060:
            reset = None  # Do not guess a timezone correction for implausible reset values.
        slot = f"{unit if type(unit) is int else 'unknown'}:{count if count is not None else 'unknown'}"
        windows.append(_window(kind, slot, entry.get("percentage"), seconds, reset))
    if len({w["id"] for w in windows}) != len(windows):
        raise ValueError("duplicate quota window")
    windows.sort(key=lambda w: (w["duration_seconds"] or MAX_DURATION + 1, w["id"]))
    plan = next((text(data.get(k)) for k in ("planName", "plan", "plan_type", "packageName", "level")
                 if text(data.get(k)) is not None), None)
    return _provider("z.AI", "coding-plan", "zai-quota-api", plan, now, windows)


def public_snapshot(providers: dict, now: float, stale_after: float = 180) -> dict:
    """Project immutable last-good observations; an expired reset never means 0%."""
    result = copy.deepcopy(providers)
    for provider in result.values():
        observed = number(provider.get("observed_at"))
        provider["stale"] = (observed is None or not 0 <= now - observed <= stale_after
                             or provider.get("status") != "ok")
        for window in provider.get("windows", []):
            reset = window.get("resets_at")
            window["expired"] = reset is not None and now >= reset
    return {"schema_version": 1, "generated_at": now, "providers": result}
