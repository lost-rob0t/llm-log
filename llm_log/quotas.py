"""Provider-reported subscription quotas, separate from token capture/admission.

One asyncio actor owns snapshots. HTTP reads never launch provider work. Only
allowlisted metadata is published; raw responses, email and credentials are not.
See roam/solutions/provider-quota-telemetry.org for source/schema provenance.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import ipaddress
import json
import math
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

MAX_BYTES = 262144
MAX_WINDOWS = 64
LABELS = {"zai": "z.AI", "gpt": "GPT"}


class QuotaError(ValueError):
    def __init__(self, code: str, retry_after: float = 60):
        super().__init__(code)
        self.code = code
        self.retry_after = max(30, min(float(retry_after), 86400))


def number(value: Any, low: float = 0, high: float = 1e15) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high or not math.isfinite(value):
        return None
    return float(value)


def text(value: Any, default: str = "unknown") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return "".join(c for c in value if c.isprintable())[:64]


def window_label(seconds: float | None) -> str:
    if seconds is None:
        return "period?"
    for size, label in ((604800, "week"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0:
            count = int(seconds / size)
            return label if label == "week" and count == 1 else f"{count}{label}"
    return f"{seconds:g}s"


def window(meter: str, slot: str, percent: Any, seconds: float | None,
           reset: Any = None, *, label: str | None = None) -> dict:
    used = number(percent, high=100)
    return {"id": f"{meter}:{slot}", "meter": meter,
            "label": label or window_label(seconds), "used_percent": used,
            "window_seconds": seconds, "resets_at": number(reset, low=1),
            "state": "ok" if used is not None else "unknown"}


def provider(provider_id: str, windows: list[dict], now: float,
             plan: Any = None) -> dict:
    return {"id": provider_id, "label": LABELS[provider_id],
            "scope": "coding-plan" if provider_id == "zai" else "account-rate-limits",
            "source": "zai-monitor" if provider_id == "zai" else "codex-app-server",
            "plan": text(plan) if plan is not None else None,
            "updated_at": now, "state": "ok", "error": None, "windows": windows}


def unavailable(provider_id: str, state: str = "unavailable") -> dict:
    result = provider(provider_id, [], 0)
    result["state"] = state
    return result


def zai_snapshot(payload: dict, now: float) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise QuotaError("invalid_response")
    data = payload.get("data", payload)
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list) or len(limits) > MAX_WINDOWS:
        raise QuotaError("invalid_response")
    windows = []
    seen = set()
    for index, row in enumerate(limits):
        if not isinstance(row, dict):
            raise QuotaError("invalid_response")
        kind = row.get("type")
        if kind not in ("TOKENS_LIMIT", "TIME_LIMIT"):
            continue
        seconds, label = None, None
        if kind == "TOKENS_LIMIT":
            period = (number(row.get("unit")), number(row.get("number")))
            if period == (3, 5) or ("unit" not in row and "number" not in row):
                seconds = 18000
            elif period == (6, 1):
                seconds = 604800
        else:
            label = "MCP month"
        raw_reset = number(row.get("nextResetTime"), low=1)
        reset = raw_reset / 1000 if raw_reset is not None and raw_reset >= 1e11 else raw_reset
        slot = str(int(seconds)) if seconds is not None else f"{kind}:{index}"
        if slot in seen:
            raise QuotaError("ambiguous_windows")
        seen.add(slot)
        windows.append(window("coding-plan", slot, row.get("percentage"), seconds,
                              reset, label=label))
    for seconds in (18000, 604800):
        if str(seconds) not in seen:
            missing = window("coding-plan", str(seconds), None, seconds)
            missing["state"] = "unavailable"
            windows.append(missing)
    windows.sort(key=lambda item: (item["window_seconds"] or 1e15, item["id"]))
    return provider("zai", windows, now, data.get("planType") or data.get("planName"))


def gpt_snapshot(payload: dict, account: dict, now: float) -> dict:
    if not isinstance(payload, dict) or not isinstance(account, dict):
        raise QuotaError("invalid_response")
    groups = payload.get("rateLimitsByLimitId")
    if groups is None:
        single = payload.get("rateLimits")
        groups = {text(single.get("limitId"), "codex"): single} if isinstance(single, dict) else {}
    if not isinstance(groups, dict) or len(groups) > MAX_WINDOWS // 2:
        raise QuotaError("invalid_response")
    windows = []
    plan = account.get("planType")
    for key, group in sorted(groups.items()):
        if not isinstance(group, dict):
            raise QuotaError("invalid_response")
        meter = text(group.get("limitId"), text(key))
        plan = plan or group.get("planType")
        for slot in ("primary", "secondary"):
            row = group.get(slot)
            if row is None:
                continue
            if not isinstance(row, dict):
                raise QuotaError("invalid_response")
            minutes = number(row.get("windowDurationMins"), low=1, high=527040)
            windows.append(window(meter, slot, row.get("usedPercent"),
                                  minutes * 60 if minutes else None, row.get("resetsAt")))
    result = provider("gpt", windows, now, plan)
    if not windows:
        result["state"] = "unavailable"
    return result


def read_key() -> str:
    key = os.environ.get("ZAI_API_KEY", "")
    filename = os.environ.get("LLM_LOG_ZAI_KEY_FILE")
    if filename:
        try:
            with Path(filename).expanduser().open("r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise QuotaError("private_key_file_required")
                key = stream.read(8193)
        except OSError:
            raise QuotaError("credentials_unavailable") from None
    key = key.strip()
    if not key or len(key) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise QuotaError("credentials_unavailable")
    return key


async def read_zai_account() -> dict:
    key = read_key()
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.get("https://api.z.ai/api/monitor/usage/quota/limit",
                               headers={"Authorization": key, "Accept": "application/json"},
                               allow_redirects=False) as response:
            if response.status != 200:
                if response.status == 429:
                    retry = response.headers.get("Retry-After", "300")
                    raise QuotaError("rate_limited", float(retry) if retry.isdigit() else 300)
                raise QuotaError("login_required" if response.status in (401, 403) else "provider_error")
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise QuotaError("response_too_large")
            try:
                payload = json.loads(chunks)
            except (ValueError, UnicodeError):
                raise QuotaError("invalid_response") from None
    return zai_snapshot(payload, time.time())


async def read_codex_account(executable: str | None = None) -> dict:
    """Use managed Codex auth over stdio. Never start a thread/turn or read tokens."""
    binary = executable or os.environ.get("LLM_LOG_CODEX_BIN", "codex")
    try:
        process = await asyncio.create_subprocess_exec(
            binary, "app-server", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_BYTES)
    except OSError:
        raise QuotaError("codex_unavailable") from None
    assert process.stdin is not None and process.stdout is not None

    async def send(message: dict) -> None:
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def request(method: str, identity: int, params: dict | None = None) -> dict:
        await send({"id": identity, "method": method, "params": params or {}})
        for _ in range(128):
            line = await process.stdout.readline()
            if not line:
                raise QuotaError("codex_disconnected")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise QuotaError("invalid_response")
            if "method" in message:
                if "id" in message:
                    await send({"id": message["id"], "error": {"code": -32601,
                               "message": "Unsupported by read-only quota client"}})
                continue
            if message.get("id") == identity:
                if "error" in message:
                    raise QuotaError("account_query_failed")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise QuotaError("invalid_response")
                return result
        raise QuotaError("message_limit")

    try:
        async with asyncio.timeout(15):
            await request("initialize", 1, {"clientInfo": {"name": "llm_log_quota", "version": "1.0.0"}})
            await send({"method": "initialized"})
            account = (await request("account/read", 2, {"refreshToken": False})).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise QuotaError("chatgpt_login_required")
            return gpt_snapshot(await request("account/rateLimits/read", 3), account, time.time())
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def atomic_snapshot(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".quota-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class QuotaActor:
    """Single writer with two bounded provider calls; GETs read snapshots only."""
    def __init__(self, adapters: dict[str, Callable[[], Awaitable[dict]]] | None = None,
                 interval: float = 60, clock: Callable[[], float] = time.time,
                 snapshot_path: Path | None = None):
        if not 30 <= interval <= 3600:
            raise ValueError("quota interval must be 30..3600 seconds")
        self.adapters = adapters if adapters is not None else {"zai": read_zai_account, "gpt": read_codex_account}
        if set(self.adapters) - LABELS.keys():
            raise ValueError("unknown quota adapter")
        self.interval, self.clock, self.snapshot_path = interval, clock, snapshot_path
        self.providers = {key: unavailable(key) for key in LABELS}
        self.next_due = {key: 0.0 for key in LABELS}
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict:
        now = self.clock()
        providers = copy.deepcopy(list(self.providers.values()))
        for item in providers:
            if item["updated_at"] and now - item["updated_at"] > self.interval * 3:
                item["state"] = "stale"
            for row in item["windows"]:
                if row["resets_at"] is not None and now >= row["resets_at"]:
                    row["state"] = "expired"
        return {"schema_version": 1, "generated_at": now, "stale_after_seconds": self.interval * 3,
                "providers": providers}

    async def refresh(self) -> None:
        async with self._lock:
            now = self.clock()
            due = [key for key in self.adapters if now >= self.next_due[key]]
            async def bounded(key: str) -> dict:
                return await asyncio.wait_for(self.adapters[key](), 20)
            results = await asyncio.gather(*(bounded(key) for key in due), return_exceptions=True)
            for key, result in zip(due, results):
                delay = self.interval
                if isinstance(result, BaseException):
                    previous = self.providers[key]
                    previous["state"] = "stale" if previous["updated_at"] else "unavailable"
                    previous["error"] = result.code if isinstance(result, QuotaError) else "provider_unavailable"
                    if isinstance(result, QuotaError):
                        delay = max(delay, result.retry_after)
                        if "login" in result.code or "credentials" in result.code:
                            self.providers[key] = {**unavailable(key), "error": result.code}
                else:
                    self.providers[key] = result
                self.next_due[key] = self.clock() + delay
            if self.snapshot_path:
                try:
                    await asyncio.to_thread(atomic_snapshot, self.snapshot_path, self.snapshot())
                except OSError:
                    # Disk persistence is optional; the in-memory API remains available.
                    pass

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.interval)


QUOTA_ACTOR = web.AppKey("quota-actor", QuotaActor)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except ValueError:
        return False


async def quotas(request: web.Request) -> web.Response:
    # No browser-origin reads, DNS-rebinding hostnames, remote peers or CORS.
    try:
        host = urlsplit("//" + request.host).hostname
    except ValueError:
        host = None
    if request.headers.get("Origin") or not _loopback(request.remote) or not _loopback(host):
        raise web.HTTPForbidden(text="quota telemetry is local-only")
    return web.json_response(request.app[QUOTA_ACTOR].snapshot(), headers={"Cache-Control": "no-store"})


def install_quota_routes(app: web.Application, *, actor: QuotaActor | None = None,
                         enabled: bool | None = None) -> None:
    enabled = os.environ.get("LLM_LOG_QUOTAS_ENABLED") == "1" if enabled is None else enabled
    if actor is None:
        filename = os.environ.get("LLM_LOG_QUOTA_SNAPSHOT")
        actor = QuotaActor(snapshot_path=Path(filename).expanduser() if filename else None)
        if not enabled:
            actor.providers = {key: unavailable(key, "disabled") for key in LABELS}
    app[QUOTA_ACTOR] = actor
    app.router.add_get("/api/v1/quotas", quotas)
    if enabled:
        async def lifecycle(_app: web.Application):
            task = asyncio.create_task(actor.run(), name="llm-log-quota-actor")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        app.cleanup_ctx.append(lifecycle)
