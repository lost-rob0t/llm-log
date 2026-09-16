from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .recovery import RecoveryCandidate, RetryRecoveryEvidence, RetryRecoveryTracker
from .routing import RoutingObservation
from .transport_errors import TransportErrorEvidence


class CorrelationActor:
    """Single owner for derived routing and retry-recovery evidence.

    Raw captures and typed transport errors remain owned by RecorderActor. This actor
    only persists projections that can be rebuilt from those append-only sources.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._queue: asyncio.Queue[
            tuple[
                str,
                RoutingObservation | TransportErrorEvidence | RecoveryCandidate | None,
                asyncio.Future[RetryRecoveryEvidence | None] | None,
            ]
        ] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._failure: BaseException | None = None

    async def start(self) -> None:
        async with self._state_lock:
            if self._task is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._task = asyncio.create_task(self._run(), name="llm-log-correlation")

    async def observe_routing(self, observation: RoutingObservation) -> None:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("routing", observation, future))
        await future

    async def observe_error(self, error: TransportErrorEvidence) -> None:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("error", error, future))
        await future

    async def observe_success(
        self,
        candidate: RecoveryCandidate,
    ) -> RetryRecoveryEvidence | None:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("success", candidate, future))
        return await future

    async def flush(self) -> None:
        if self._task is None:
            return
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("flush", None, future))
        await future

    async def close(self) -> None:
        async with self._state_lock:
            task = self._task
            if task is None:
                return
            future = asyncio.get_running_loop().create_future()
            await self._queue.put(("close", None, future))
            await future
            await task
            self._task = None

    @staticmethod
    def _write_jsonl(handle, payload: dict[str, Any]) -> None:
        handle.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        handle.flush()

    async def _run(self) -> None:
        tracker = RetryRecoveryTracker.load(self.root)
        routing_path = self.root / "routing.jsonl"
        recoveries_path = self.root / "recoveries.jsonl"

        with (
            routing_path.open("a", encoding="utf-8", buffering=1) as routing,
            recoveries_path.open("a", encoding="utf-8", buffering=1) as recoveries,
        ):
            while True:
                op, payload, future = await self._queue.get()
                try:
                    if self._failure is not None and op != "close":
                        raise self._failure

                    result: RetryRecoveryEvidence | None = None
                    if op == "routing":
                        assert isinstance(payload, RoutingObservation)
                        self._write_jsonl(routing, payload.as_json())
                        tracker.observe_routing(payload)
                    elif op == "error":
                        assert isinstance(payload, TransportErrorEvidence)
                        tracker.observe_error(payload)
                    elif op == "success":
                        assert isinstance(payload, RecoveryCandidate)
                        result = tracker.match_success(payload)
                        if result is not None:
                            self._write_jsonl(recoveries, result.as_json())
                            tracker.commit_recovery(result)
                    elif op == "flush":
                        routing.flush()
                        recoveries.flush()
                    elif op == "close":
                        routing.flush()
                        recoveries.flush()
                        if future is not None and not future.done():
                            future.set_result(None)
                        return
                    else:
                        raise RuntimeError(f"unknown correlation actor message: {op}")
                except BaseException as exc:
                    self._failure = exc
                    if future is not None and not future.done():
                        future.set_exception(exc)
                else:
                    if future is not None and not future.done():
                        future.set_result(result)
                finally:
                    self._queue.task_done()
