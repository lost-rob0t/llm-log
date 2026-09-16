from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .routing import RoutingObservation
from .transport_errors import TransportErrorEvidence

_DEFAULT_WINDOW_SECONDS = 10.0
_MAX_PENDING_FAILURES = 4096
_MAX_ROUTING_OBSERVATIONS = 8192


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    event_id: str
    started_at: str
    completed_at: str
    request_sha256: str
    routing_observation_id: str | None = None
    selected_provider: str | None = None


@dataclass(frozen=True, slots=True)
class RetryRecoveryEvidence:
    recovery_id: str
    observed_at: str
    outcome: str
    failed_event_id: str
    retry_event_id: str
    request_sha256: str
    failed_error_class: str
    retry_delay_ms: int
    failed_routing_observation_id: str | None = None
    retry_routing_observation_id: str | None = None
    failed_selected_provider: str | None = None
    retry_selected_provider: str | None = None
    provider_changed: bool | None = None
    source: str = "proxy_retry_correlation"

    def as_json(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class _PendingFailure:
    event_id: str
    observed_at: str
    request_sha256: str
    error_class: str
    routing_observation_id: str | None
    selected_provider: str | None


def _parse_time(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_jsonl(path: Path, *, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    tail: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    tail.append(value)
    except OSError:
        return []
    return list(tail)


class RetryRecoveryTracker:
    def __init__(self, *, window_seconds: float = _DEFAULT_WINDOW_SECONDS) -> None:
        if window_seconds <= 0:
            raise ValueError("recovery window must be positive")
        self.window_seconds = float(window_seconds)
        self._routing_by_event: dict[str, RoutingObservation] = {}
        self._routing_order: deque[str] = deque()
        self._pending_by_sha: dict[str, deque[_PendingFailure]] = defaultdict(deque)
        self._pending_order: deque[tuple[str, str]] = deque()
        self._consumed_failure_ids: set[str] = set()

    @classmethod
    def load(cls, root: str | Path, *, window_seconds: float = _DEFAULT_WINDOW_SECONDS) -> "RetryRecoveryTracker":
        tracker = cls(window_seconds=window_seconds)
        root = Path(root)

        for raw in _read_jsonl(root / "routing.jsonl", limit=_MAX_ROUTING_OBSERVATIONS):
            observation = tracker._routing_from_json(raw)
            if observation is not None:
                tracker.observe_routing(observation)

        for raw in _read_jsonl(root / "recoveries.jsonl", limit=_MAX_PENDING_FAILURES):
            failed_event_id = raw.get("failed_event_id")
            if isinstance(failed_event_id, str) and failed_event_id:
                tracker._consumed_failure_ids.add(failed_event_id)

        for raw in _read_jsonl(root / "errors.jsonl", limit=_MAX_PENDING_FAILURES):
            error = tracker._error_from_json(raw)
            if error is None or error.event_id in tracker._consumed_failure_ids:
                continue
            tracker.observe_error(error)

        return tracker

    @staticmethod
    def _routing_from_json(raw: dict[str, Any]) -> RoutingObservation | None:
        routing_id = raw.get("routing_id")
        event_id = raw.get("event_id")
        observed_at = raw.get("observed_at")
        router = raw.get("router")
        if not all(isinstance(value, str) and value for value in (routing_id, event_id, observed_at, router)):
            return None
        selected_provider = raw.get("selected_provider")
        if not isinstance(selected_provider, str):
            selected_provider = None
        return RoutingObservation(
            routing_id=routing_id,
            event_id=event_id,
            observed_at=observed_at,
            router=router,
            selected_provider=selected_provider,
            attempts=(),
            source=str(raw.get("source") or "provider_metadata"),
        )

    @staticmethod
    def _error_from_json(raw: dict[str, Any]) -> TransportErrorEvidence | None:
        required = {
            "error_id": raw.get("error_id"),
            "event_id": raw.get("event_id"),
            "observed_at": raw.get("observed_at"),
            "error_class": raw.get("error_class"),
            "domain": raw.get("domain"),
            "severity": raw.get("severity"),
            "attribution_scope": raw.get("attribution_scope"),
            "transport": raw.get("transport"),
        }
        if not all(isinstance(value, str) and value for value in required.values()):
            return None
        return TransportErrorEvidence(
            error_id=required["error_id"],
            event_id=required["event_id"],
            observed_at=required["observed_at"],
            error_class=required["error_class"],
            domain=required["domain"],
            severity=required["severity"],
            attribution_scope=required["attribution_scope"],
            provider=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
            model=raw.get("model") if isinstance(raw.get("model"), str) else None,
            transport=required["transport"],
            response_status=raw.get("response_status") if isinstance(raw.get("response_status"), int) else None,
            status_kind=raw.get("status_kind") if isinstance(raw.get("status_kind"), str) else None,
            error_code=raw.get("error_code") if isinstance(raw.get("error_code"), (int, str)) else None,
            request_sha256=raw.get("request_sha256") if isinstance(raw.get("request_sha256"), str) else None,
            routing_observation_id=(
                raw.get("routing_observation_id")
                if isinstance(raw.get("routing_observation_id"), str)
                else None
            ),
            source=str(raw.get("source") or "proxy"),
        )

    def observe_routing(self, observation: RoutingObservation) -> None:
        self._routing_by_event[observation.event_id] = observation
        self._routing_order.append(observation.event_id)
        while len(self._routing_order) > _MAX_ROUTING_OBSERVATIONS:
            expired_event_id = self._routing_order.popleft()
            if expired_event_id not in self._routing_order:
                self._routing_by_event.pop(expired_event_id, None)

    def observe_error(self, error: TransportErrorEvidence) -> None:
        if error.attribution_scope == "client" or not error.request_sha256:
            return
        if error.event_id in self._consumed_failure_ids:
            return

        routing = self._routing_by_event.get(error.event_id)
        pending = _PendingFailure(
            event_id=error.event_id,
            observed_at=error.observed_at,
            request_sha256=error.request_sha256,
            error_class=error.error_class,
            routing_observation_id=(
                error.routing_observation_id
                or (routing.routing_id if routing is not None else None)
            ),
            selected_provider=(routing.selected_provider if routing is not None else None),
        )
        bucket = self._pending_by_sha[pending.request_sha256]
        bucket.append(pending)
        self._pending_order.append((pending.request_sha256, pending.event_id))
        self._trim_pending()

    def _trim_pending(self) -> None:
        while len(self._pending_order) > _MAX_PENDING_FAILURES:
            sha, event_id = self._pending_order.popleft()
            bucket = self._pending_by_sha.get(sha)
            if bucket is None:
                continue
            filtered = deque(item for item in bucket if item.event_id != event_id)
            if filtered:
                self._pending_by_sha[sha] = filtered
            else:
                self._pending_by_sha.pop(sha, None)

    def match_success(self, candidate: RecoveryCandidate) -> RetryRecoveryEvidence | None:
        retry_started = _parse_time(candidate.started_at)
        if retry_started is None:
            return None
        bucket = self._pending_by_sha.get(candidate.request_sha256)
        if not bucket:
            return None

        matched: _PendingFailure | None = None
        matched_delay = 0.0
        for pending in reversed(bucket):
            failed_at = _parse_time(pending.observed_at)
            if failed_at is None:
                continue
            delay = (retry_started - failed_at).total_seconds()
            if delay < 0 or delay > self.window_seconds:
                continue
            matched = pending
            matched_delay = delay
            break
        if matched is None:
            return None

        retry_routing = self._routing_by_event.get(candidate.event_id)
        retry_provider = (
            candidate.selected_provider
            or (retry_routing.selected_provider if retry_routing is not None else None)
        )
        failed_provider = matched.selected_provider
        provider_changed = (
            failed_provider != retry_provider
            if failed_provider is not None and retry_provider is not None
            else None
        )
        return RetryRecoveryEvidence(
            recovery_id=f"recovery:{matched.event_id}:{candidate.event_id}",
            observed_at=candidate.completed_at,
            outcome="recovered",
            failed_event_id=matched.event_id,
            retry_event_id=candidate.event_id,
            request_sha256=candidate.request_sha256,
            failed_error_class=matched.error_class,
            retry_delay_ms=max(0, round(matched_delay * 1000)),
            failed_routing_observation_id=matched.routing_observation_id,
            retry_routing_observation_id=(
                candidate.routing_observation_id
                or (retry_routing.routing_id if retry_routing is not None else None)
            ),
            failed_selected_provider=failed_provider,
            retry_selected_provider=retry_provider,
            provider_changed=provider_changed,
        )

    def commit_recovery(self, recovery: RetryRecoveryEvidence) -> None:
        self._consumed_failure_ids.add(recovery.failed_event_id)
        bucket = self._pending_by_sha.get(recovery.request_sha256)
        if bucket is None:
            return
        filtered = deque(
            pending
            for pending in bucket
            if pending.event_id != recovery.failed_event_id
        )
        if filtered:
            self._pending_by_sha[recovery.request_sha256] = filtered
        else:
            self._pending_by_sha.pop(recovery.request_sha256, None)
