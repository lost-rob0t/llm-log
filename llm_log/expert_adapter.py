from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Sequence


class ExpertAdapterError(RuntimeError):
    """The Common Lisp expert subprocess could not complete an exchange."""


class SubprocessExpertPlane:
    """Serialized JSON-lines adapter for the long-lived Common Lisp expert service.

    The adapter exposes declared operations only. It never accepts raw Prolog goals and
    never replays an exchange after an ambiguous timeout, EOF, or malformed reply.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        data_dir: Path,
        timeout: float = 1.0,
        append_service_args: bool = True,
    ) -> None:
        if not command:
            raise ValueError("expert command must not be empty")
        if timeout <= 0:
            raise ValueError("expert timeout must be positive")
        self._command = tuple(str(part) for part in command)
        self._data_dir = Path(data_dir)
        self._timeout = float(timeout)
        self._append_service_args = append_service_args
        self._process: asyncio.subprocess.Process | None = None
        self._exchange_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        if self._process is not None:
            await self.close()

        self._data_dir.mkdir(parents=True, exist_ok=True)
        command = list(self._command)
        if self._append_service_args:
            command.extend(("serve", "--stdio", "--data-dir", str(self._data_dir)))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._process = None
            raise ExpertAdapterError("expert service unavailable") from exc

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self._timeout)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._timeout)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def health(self) -> dict[str, Any]:
        return await self._request("health", {})

    async def _request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._exchange_lock:
            if not self.running:
                raise ExpertAdapterError("expert service is not running")
            process = self._process
            assert process is not None
            assert process.stdin is not None
            assert process.stdout is not None

            request = {"version": 1, "operation": operation, "payload": payload}
            encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode()
            try:
                process.stdin.write(encoded)
                await asyncio.wait_for(process.stdin.drain(), timeout=self._timeout)
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=self._timeout)
            except asyncio.TimeoutError as exc:
                await self._invalidate(process)
                raise ExpertAdapterError("expert service timed out") from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                await self._invalidate(process)
                raise ExpertAdapterError("expert service pipe failed") from exc

            if not raw:
                await self._invalidate(process)
                raise ExpertAdapterError("expert service closed the protocol stream")

            try:
                reply = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await self._invalidate(process)
                raise ExpertAdapterError("expert service returned malformed JSON") from exc

            if not isinstance(reply, dict) or reply.get("status") not in {"ok", "error"}:
                await self._invalidate(process)
                raise ExpertAdapterError("expert service returned malformed reply")
            if reply["status"] == "error":
                error = reply.get("error")
                code = error.get("code") if isinstance(error, dict) else "expert_error"
                raise ExpertAdapterError(f"expert service rejected operation: {code}")

            result = reply.get("result")
            if not isinstance(result, dict):
                await self._invalidate(process)
                raise ExpertAdapterError("expert service returned malformed result")
            return result

    async def _invalidate(self, process: asyncio.subprocess.Process) -> None:
        if self._process is process:
            self._process = None
        if process.returncode is None:
            process.kill()
        try:
            await process.wait()
        except ProcessLookupError:
            pass
