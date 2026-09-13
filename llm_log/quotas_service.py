"""Read-only local subscription telemetry. One actor owns the published snapshot."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .quotas import normalize_codex, normalize_zai, public_snapshot

MAX_BYTES = 65536
ZAI_ENDPOINT = "https://api.z.ai/api/monitor/usage/quota/limit"
ACTOR = web.AppKey("quota-actor", object)


class QuotaUnavailable(Exception):
    """Only a safe, fixed reason code may cross the actor/API boundary."""


def cache_path() -> Path:
    override = os.environ.get("LLM_LOG_QUOTA_CACHE")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "llm-log/quotas.json"


def write_snapshot(path: Path, data: dict) -> None:
    encoded = json.dumps(data, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("quota snapshot too large")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".quota-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read_zai_key(key_file: Path | None = None) -> str:
    if key_file is not None:
        try:
            with key_file.expanduser().open("r", encoding="utf-8") as stream:
                token = stream.read(4097).strip()
        except OSError:
            raise QuotaUnavailable("not-configured") from None
    else:
        token = os.environ.get("Z_AI_API_KEY") or os.environ.get("ZAI_API_KEY") or ""
    if not token:
        raise QuotaUnavailable("not-configured")
    if len(token) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise QuotaUnavailable("invalid-credential")
    return token


async def fetch_zai(session: ClientSession, key_file: Path | None = None) -> dict:
    token = read_zai_key(key_file)
    # Use the fixed official host, never a request-selected URL. No auth-bearing redirects.
    async with session.get(ZAI_ENDPOINT, headers={"Authorization": token, "Accept": "application/json"},
                           allow_redirects=False, timeout=ClientTimeout(total=12)) as response:
        if response.status != 200:
            raise QuotaUnavailable("auth" if response.status in (401, 403) else "provider-unavailable")
        try:
            body = await response.content.readexactly(MAX_BYTES + 1)
        except asyncio.IncompleteReadError as exc:
            body = exc.partial
        if len(body) > MAX_BYTES:
            raise QuotaUnavailable("invalid-response")
        try:
            return normalize_zai(json.loads(body), time.time())
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            raise QuotaUnavailable("invalid-response") from None


async def fetch_codex(command: str = "codex") -> dict:
    """Query supported read-only RPCs using the user's existing Codex login.

    No thread or turn is started; no prompts or browser tokens are collected.
    Stderr is deliberately discarded because provider errors may include identity.
    """
    proc = await asyncio.create_subprocess_exec(
        command, "app-server", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_BYTES + 1)
    assert proc.stdin is not None and proc.stdout is not None
    consumed = 0

    async def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        await proc.stdin.drain()

    async def rpc(request_id: int, method: str, params: dict) -> dict:
        nonlocal consumed
        await send({"id": request_id, "method": method, "params": params})
        for _ in range(128):
            line = await proc.stdout.readline()
            consumed += len(line)
            if not line or consumed > MAX_BYTES:
                raise QuotaUnavailable("invalid-response")
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise QuotaUnavailable("invalid-response") from None
            if not isinstance(message, dict):
                raise QuotaUnavailable("invalid-response")
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise QuotaUnavailable("rpc-unavailable")
            return message["result"]
        raise QuotaUnavailable("invalid-response")

    try:
        async with asyncio.timeout(20):
            await rpc(0, "initialize", {"clientInfo": {"name": "llm_log_quotas", "title": "llm-log quotas", "version": "1.0.0"}})
            await send({"method": "initialized", "params": {}})
            account = await rpc(1, "account/read", {"refreshToken": False})
            identity = account.get("account")
            if not isinstance(identity, dict) or identity.get("type") != "chatgpt":
                raise QuotaUnavailable("login-required")
            quotas = await rpc(2, "account/rateLimits/read", {})
            return normalize_codex(quotas, account, time.time())
    finally:
        proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()


class QuotaActor:
    """Provider tasks send bounded messages; only run() mutates observation state."""

    def __init__(self, path: Path | None, stale_after: float = 180):
        self.path = path
        self.stale_after = stale_after
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=4)
        self.providers = {
            key: {"label": label, "scope": scope, "plan": None, "windows": [],
                  "observed_at": None, "status": "starting"}
            for key, label, scope in (("zai", "z.AI", "coding-plan"), ("gpt", "GPT", "codex"))
        }

    def snapshot(self) -> dict:
        return public_snapshot(self.providers, time.time(), self.stale_after)

    async def run(self) -> None:
        while True:
            key, observation, error = await self.inbox.get()
            try:
                if observation is not None:
                    self.providers[key] = observation
                else:
                    previous = self.providers[key]
                    self.providers[key] = {**previous, "status": error or "unavailable"}
                if self.path is not None:
                    try:
                        await asyncio.to_thread(write_snapshot, self.path, self.snapshot())
                    except OSError:
                        # The live API remains useful even with an unwritable optional desktop cache.
                        pass
            finally:
                self.inbox.task_done()


async def poll_provider(actor: QuotaActor, key: str, fetch: Callable[[], Awaitable[dict]], interval: float) -> None:
    failures = 0
    while True:
        try:
            observation = await fetch()
            error, failures = None, 0
        except asyncio.CancelledError:
            raise
        except QuotaUnavailable as exc:
            observation, error = None, str(exc)
            failures += 1
        except (ClientError, TimeoutError, OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            observation, error = None, "unavailable"
            failures += 1
        await actor.inbox.put((key, observation, error))
        # Bounded exponential backoff; refreshing a bar never triggers a provider request.
        await asyncio.sleep(min(interval * 2 ** min(failures, 4), 900))


async def quotas(request: web.Request) -> web.Response:
    if request.headers.get("Origin"):
        raise web.HTTPForbidden(text="browser origins are not accepted")
    # Prevent DNS rebinding against the credential-owning loopback service.
    if request.host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
        raise web.HTTPForbidden(text="loopback host required")
    return web.json_response(request.app[ACTOR].snapshot(), headers={"Cache-Control": "no-store"})


def build_quota_app(*, path: Path | None = None, codex: str = "codex", zai_key_file: Path | None = None,
                    interval: float = 60) -> web.Application:
    if not 30 <= interval <= 900:
        raise ValueError("refresh interval must be between 30 and 900 seconds")
    app = web.Application(client_max_size=1024)
    actor = QuotaActor(path, stale_after=max(180, interval * 3))
    app[ACTOR] = actor
    app.router.add_get("/api/v1/quotas", quotas)

    async def lifecycle(_app):
        async with ClientSession(trust_env=False) as session:
            tasks = [asyncio.create_task(actor.run(), name="quota-store"),
                     asyncio.create_task(poll_provider(actor, "zai", lambda: fetch_zai(session, zai_key_file), interval)),
                     asyncio.create_task(poll_provider(actor, "gpt", lambda: fetch_codex(codex), interval))]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Local provider-reported subscription quota telemetry")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--refresh", type=float, default=60)
    parser.add_argument("--codex", default="codex", help="Codex executable, not a shell command")
    parser.add_argument("--zai-key-file", type=Path, default=os.environ.get("LLM_LOG_ZAI_KEY_FILE"))
    parser.add_argument("--cache", type=Path, default=cache_path())
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 30 <= args.refresh <= 900:
        parser.error("invalid port or refresh interval")
    app = build_quota_app(path=args.cache, codex=args.codex, zai_key_file=args.zai_key_file, interval=args.refresh)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
