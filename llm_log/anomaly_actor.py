from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from .tool_call_anomaly import ToolCallAnomaly, analyze_tool_calls


@dataclass(frozen=True, slots=True)
class CompletedModelObservation:
    event_id: str
    provider: str
    model: str
    request_body: bytes
    response_body: bytes
    observed_quantization: str | None = None


class AnomalyActor:
    """Single writer for observe-only model-behavior anomaly projections."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._queue: asyncio.Queue[
            tuple[
                str,
                CompletedModelObservation | None,
                asyncio.Future[list[ToolCallAnomaly] | None] | None,
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
            self._task = asyncio.create_task(self._run(), name="llm-log-anomaly")

    async def observe(
        self,
        observation: CompletedModelObservation,
    ) -> list[ToolCallAnomaly]:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("observe", observation, future))
        result = await future
        return result or []

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
    def _write_jsonl(handle, anomaly: ToolCallAnomaly) -> None:
        handle.write(
            json.dumps(
                anomaly.as_json(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        handle.flush()

    async def _run(self) -> None:
        path = self.root / "anomalies.jsonl"
        with path.open("a", encoding="utf-8", buffering=1) as handle:
            while True:
                op, payload, future = await self._queue.get()
                try:
                    if self._failure is not None and op != "close":
                        raise self._failure

                    result: list[ToolCallAnomaly] | None = None
                    if op == "observe":
                        assert isinstance(payload, CompletedModelObservation)
                        result = analyze_tool_calls(
                            event_id=payload.event_id,
                            provider=payload.provider,
                            model=payload.model,
                            request_body=payload.request_body,
                            response_body=payload.response_body,
                            observed_quantization=payload.observed_quantization,
                        )
                        for anomaly in result:
                            self._write_jsonl(handle, anomaly)
                    elif op == "flush":
                        handle.flush()
                    elif op == "close":
                        handle.flush()
                        if future is not None and not future.done():
                            future.set_result(None)
                        return
                    else:
                        raise RuntimeError(f"unknown anomaly actor message: {op}")
                except BaseException as exc:
                    self._failure = exc
                    if future is not None and not future.done():
                        future.set_exception(exc)
                else:
                    if future is not None and not future.done():
                        future.set_result(result)
                finally:
                    self._queue.task_done()
