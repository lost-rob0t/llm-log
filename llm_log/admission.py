from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    max_active: int = 4
    max_queue_depth: int = 32
    queue_timeout_seconds: float = 10.0
    requests_per_minute: float = 60.0
    burst: int = 4
    retry_after_seconds: int = 1
    provider_groups: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_active <= 0:
            raise ValueError("max_active must be positive")
        if self.max_queue_depth < 0:
            raise ValueError("max_queue_depth must be non-negative")
        if self.queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        if self.requests_per_minute < 0:
            raise ValueError("requests_per_minute must be non-negative")
        if self.burst <= 0:
            raise ValueError("burst must be positive")
        if self.retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        for provider, group in self.provider_groups.items():
            if not provider or not group:
                raise ValueError("provider group names must be non-empty")


class AdmissionRejected(RuntimeError):
    def __init__(self, reason: str, retry_after: int):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = max(1, int(retry_after))


@dataclass(slots=True)
class _Waiter:
    deadline: float


@dataclass(slots=True)
class _AdmissionState:
    active: int
    queue: deque[_Waiter]
    tokens: float
    last_refill: float
    condition: asyncio.Condition


class AdmissionLease:
    def __init__(self, scheduler: "AdmissionScheduler", key: str):
        self._scheduler = scheduler
        self._key = key
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._scheduler._release(self._key)

    async def __aenter__(self) -> "AdmissionLease":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class AdmissionScheduler:
    """One process-local admission owner for provider/quota groups.

    A request consumes one active slot and, when rate limiting is enabled, one
    request token. Releasing the active slot never refunds the token.
    """

    def __init__(self, policy: AdmissionPolicy, *, clock=time.monotonic):
        self.policy = AdmissionPolicy(
            max_active=policy.max_active,
            max_queue_depth=policy.max_queue_depth,
            queue_timeout_seconds=policy.queue_timeout_seconds,
            requests_per_minute=policy.requests_per_minute,
            burst=policy.burst,
            retry_after_seconds=policy.retry_after_seconds,
            provider_groups=dict(policy.provider_groups),
        )
        self._clock = clock
        self._states: dict[str, _AdmissionState] = {}

    def _key(self, provider: str) -> str:
        return self.policy.provider_groups.get(provider, provider)

    def _state(self, key: str) -> _AdmissionState:
        state = self._states.get(key)
        if state is None:
            now = self._clock()
            tokens = (
                float(self.policy.burst)
                if self.policy.requests_per_minute > 0
                else math.inf
            )
            state = _AdmissionState(
                active=0,
                queue=deque(),
                tokens=tokens,
                last_refill=now,
                condition=asyncio.Condition(),
            )
            self._states[key] = state
        return state

    def _refill(self, state: _AdmissionState, now: float) -> None:
        if self.policy.requests_per_minute <= 0:
            state.tokens = math.inf
            state.last_refill = now
            return
        effective_now = max(now, state.last_refill)
        elapsed = effective_now - state.last_refill
        refill_rate = self.policy.requests_per_minute / 60.0
        state.tokens = min(
            float(self.policy.burst),
            state.tokens + elapsed * refill_rate,
        )
        state.last_refill = effective_now

    def _capacity(self, state: _AdmissionState) -> bool:
        return (
            state.active < self.policy.max_active
            and state.tokens >= 1.0
        )

    def _consume(self, state: _AdmissionState) -> None:
        state.active += 1
        if self.policy.requests_per_minute > 0:
            state.tokens -= 1.0

    def _rate_wait(self, state: _AdmissionState) -> float:
        if self.policy.requests_per_minute <= 0 or state.tokens >= 1.0:
            return 0.0
        refill_rate = self.policy.requests_per_minute / 60.0
        return (1.0 - state.tokens) / refill_rate

    def _retry_after(self, state: _AdmissionState) -> int:
        return max(
            self.policy.retry_after_seconds,
            math.ceil(self._rate_wait(state)),
        )

    async def acquire(self, provider: str) -> AdmissionLease:
        key = self._key(provider)
        state = self._state(key)
        waiter: _Waiter | None = None

        async with state.condition:
            now = self._clock()
            self._refill(state, now)
            if not state.queue and self._capacity(state):
                self._consume(state)
                return AdmissionLease(self, key)

            if len(state.queue) >= self.policy.max_queue_depth:
                raise AdmissionRejected("queue_full", self._retry_after(state))

            waiter = _Waiter(deadline=now + self.policy.queue_timeout_seconds)
            state.queue.append(waiter)

            try:
                while True:
                    now = self._clock()
                    self._refill(state, now)

                    if now >= waiter.deadline:
                        if waiter in state.queue:
                            state.queue.remove(waiter)
                        state.condition.notify_all()
                        raise AdmissionRejected(
                            "queue_timeout", self._retry_after(state)
                        )

                    if state.queue and state.queue[0] is waiter and self._capacity(state):
                        state.queue.popleft()
                        self._consume(state)
                        state.condition.notify_all()
                        return AdmissionLease(self, key)

                    wake_after = waiter.deadline - now
                    if (
                        state.queue
                        and state.queue[0] is waiter
                        and state.active < self.policy.max_active
                    ):
                        rate_wait = self._rate_wait(state)
                        if rate_wait > 0:
                            wake_after = min(wake_after, rate_wait)

                    try:
                        await asyncio.wait_for(
                            state.condition.wait(),
                            timeout=max(0.001, wake_after),
                        )
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                if waiter in state.queue:
                    state.queue.remove(waiter)
                    state.condition.notify_all()
                raise

    async def _release(self, key: str) -> None:
        state = self._state(key)
        async with state.condition:
            if state.active <= 0:
                raise RuntimeError("admission active count underflow")
            state.active -= 1
            state.condition.notify_all()

    async def snapshot(self, provider: str) -> dict[str, int | float | str]:
        key = self._key(provider)
        state = self._state(key)
        async with state.condition:
            self._refill(state, self._clock())
            return {
                "group": key,
                "active": state.active,
                "queued": len(state.queue),
                "tokens": (
                    state.tokens
                    if self.policy.requests_per_minute > 0
                    else -1.0
                ),
            }
