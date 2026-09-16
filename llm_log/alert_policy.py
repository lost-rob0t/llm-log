from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .recovery import RetryRecoveryEvidence
from .transport_errors import TransportErrorEvidence


@dataclass(frozen=True, slots=True)
class AlertPolicyRule:
    action: str
    severity: str
    rate_class: str
    grace_seconds: int
    rate_window_seconds: int
    max_notifications_per_window: int
    reason: str


@dataclass(frozen=True, slots=True)
class AlertDecision:
    decision_id: str
    event_id: str
    observed_at: str
    action: str
    severity: str
    rate_class: str
    grace_seconds: int
    rate_window_seconds: int
    max_notifications_per_window: int
    reason: str
    error_class: str | None = None
    attribution_scope: str | None = None
    response_status: int | None = None
    model: str | None = None
    recovery_id: str | None = None
    source: str = "transport_alert_policy"

    def as_json(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


_CLIENT_RULE = AlertPolicyRule(
    action="log_only",
    severity="info",
    rate_class="none",
    grace_seconds=0,
    rate_window_seconds=0,
    max_notifications_per_window=0,
    reason="client_disconnect_not_provider_failure",
)

_ACCOUNT_GATEWAY_RULE = AlertPolicyRule(
    action="alert",
    severity="warning",
    rate_class="account_gateway",
    grace_seconds=0,
    rate_window_seconds=900,
    max_notifications_per_window=1,
    reason="account_or_gateway_failure",
)

_PROVIDER_TRANSIENT_RULE = AlertPolicyRule(
    action="alert_if_unrecovered",
    severity="warning",
    rate_class="provider_transient",
    grace_seconds=10,
    rate_window_seconds=60,
    max_notifications_per_window=1,
    reason="transient_failure_wait_for_retry_recovery",
)

_PROTOCOL_INTEGRITY_RULE = AlertPolicyRule(
    action="alert_if_unrecovered",
    severity="warning",
    rate_class="protocol_integrity",
    grace_seconds=10,
    rate_window_seconds=300,
    max_notifications_per_window=1,
    reason="stream_integrity_failure_wait_for_retry_recovery",
)

_UNKNOWN_TRANSPORT_RULE = AlertPolicyRule(
    action="alert",
    severity="warning",
    rate_class="unknown_transport",
    grace_seconds=0,
    rate_window_seconds=60,
    max_notifications_per_window=1,
    reason="unknown_transport_failure",
)

_TRANSIENT_CLASSES = frozenset(
    {
        "upstream_connection_reset",
        "router_error",
        "upstream_stream_error",
        "upstream_http_error",
    }
)


def policy_for_error(error: TransportErrorEvidence) -> AlertPolicyRule:
    if error.attribution_scope == "client" or error.error_class == "downstream_client_disconnect":
        return _CLIENT_RULE
    if error.attribution_scope == "gateway_or_account":
        return _ACCOUNT_GATEWAY_RULE
    if error.error_class == "stream_protocol_error":
        return _PROTOCOL_INTEGRITY_RULE
    if error.error_class in _TRANSIENT_CLASSES:
        return _PROVIDER_TRANSIENT_RULE
    return _UNKNOWN_TRANSPORT_RULE


def decision_for_error(error: TransportErrorEvidence) -> AlertDecision:
    rule = policy_for_error(error)
    return AlertDecision(
        decision_id=f"alert-decision:{error.error_id}",
        event_id=error.event_id,
        observed_at=error.observed_at,
        action=rule.action,
        severity=rule.severity,
        rate_class=rule.rate_class,
        grace_seconds=rule.grace_seconds,
        rate_window_seconds=rule.rate_window_seconds,
        max_notifications_per_window=rule.max_notifications_per_window,
        reason=rule.reason,
        error_class=error.error_class,
        attribution_scope=error.attribution_scope,
        response_status=error.response_status,
        model=error.model,
    )


def decision_for_recovery(recovery: RetryRecoveryEvidence) -> AlertDecision:
    return AlertDecision(
        decision_id=f"alert-decision:{recovery.recovery_id}:suppress",
        event_id=recovery.failed_event_id,
        observed_at=recovery.observed_at,
        action="suppress_recovered",
        severity="info",
        rate_class="none",
        grace_seconds=0,
        rate_window_seconds=0,
        max_notifications_per_window=0,
        reason="recovered_within_retry_correlation",
        error_class=recovery.failed_error_class,
        recovery_id=recovery.recovery_id,
    )


class AlertPolicyActor:
    """Serialized owner for safe alert-policy decision projections."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._queue: asyncio.Queue[
            tuple[
                str,
                TransportErrorEvidence | RetryRecoveryEvidence | None,
                asyncio.Future[AlertDecision | None] | None,
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
            self._task = asyncio.create_task(self._run(), name="llm-log-alert-policy")

    async def observe_error(self, error: TransportErrorEvidence) -> AlertDecision:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("error", error, future))
        result = await future
        assert result is not None
        return result

    async def observe_recovery(self, recovery: RetryRecoveryEvidence) -> AlertDecision:
        await self.start()
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(("recovery", recovery, future))
        result = await future
        assert result is not None
        return result

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
    def _write_jsonl(handle, decision: AlertDecision) -> None:
        handle.write(
            json.dumps(
                decision.as_json(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        handle.flush()

    async def _run(self) -> None:
        path = self.root / "alert-decisions.jsonl"
        with path.open("a", encoding="utf-8", buffering=1) as handle:
            while True:
                op, payload, future = await self._queue.get()
                try:
                    if self._failure is not None and op != "close":
                        raise self._failure

                    decision: AlertDecision | None = None
                    if op == "error":
                        assert isinstance(payload, TransportErrorEvidence)
                        decision = decision_for_error(payload)
                        self._write_jsonl(handle, decision)
                    elif op == "recovery":
                        assert isinstance(payload, RetryRecoveryEvidence)
                        decision = decision_for_recovery(payload)
                        self._write_jsonl(handle, decision)
                    elif op == "flush":
                        handle.flush()
                    elif op == "close":
                        handle.flush()
                        if future is not None and not future.done():
                            future.set_result(None)
                        return
                    else:
                        raise RuntimeError(f"unknown alert-policy actor message: {op}")
                except BaseException as exc:
                    self._failure = exc
                    if future is not None and not future.done():
                        future.set_exception(exc)
                else:
                    if future is not None and not future.done():
                        future.set_result(decision)
                finally:
                    self._queue.task_done()
