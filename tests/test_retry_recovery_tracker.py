import unittest

from llm_log.recovery import RecoveryCandidate, RetryRecoveryTracker
from llm_log.routing import RoutingObservation
from llm_log.transport_errors import TransportErrorEvidence


def error(*, event_id="failed", observed_at="2026-09-16T16:00:01+00:00", sha="a" * 64):
    return TransportErrorEvidence(
        error_id=f"transport:{event_id}:router_error",
        event_id=event_id,
        observed_at=observed_at,
        error_class="router_error",
        domain="transport",
        severity="warning",
        attribution_scope="gateway",
        provider="openrouter",
        model="fixture/model",
        transport="http",
        response_status=200,
        status_kind="upstream",
        error_code=504,
        request_sha256=sha,
        routing_observation_id=f"routing:{event_id}",
    )


def routing(event_id, provider):
    return RoutingObservation(
        routing_id=f"routing:{event_id}",
        event_id=event_id,
        observed_at="2026-09-16T16:00:01+00:00",
        router="openrouter",
        selected_provider=provider,
        attempts=(),
    )


def candidate(*, event_id="retry", started_at="2026-09-16T16:00:02+00:00", sha="a" * 64):
    return RecoveryCandidate(
        event_id=event_id,
        started_at=started_at,
        completed_at=started_at,
        request_sha256=sha,
        routing_observation_id=f"routing:{event_id}",
        selected_provider="ProviderB",
    )


class RetryRecoveryTrackerTest(unittest.TestCase):
    def test_different_request_hash_never_recovers_failure(self):
        tracker = RetryRecoveryTracker(window_seconds=10)
        tracker.observe_routing(routing("failed", "ProviderA"))
        tracker.observe_error(error())

        recovery = tracker.match_success(candidate(sha="b" * 64))

        self.assertIsNone(recovery)

    def test_retry_outside_window_does_not_recover_failure(self):
        tracker = RetryRecoveryTracker(window_seconds=10)
        tracker.observe_routing(routing("failed", "ProviderA"))
        tracker.observe_error(error())

        recovery = tracker.match_success(
            candidate(started_at="2026-09-16T16:00:12.001+00:00")
        )

        self.assertIsNone(recovery)

    def test_client_disconnect_never_becomes_recovery_candidate(self):
        tracker = RetryRecoveryTracker(window_seconds=10)
        client_error = TransportErrorEvidence(
            error_id="transport:client:downstream_client_disconnect",
            event_id="client",
            observed_at="2026-09-16T16:00:01+00:00",
            error_class="downstream_client_disconnect",
            domain="transport",
            severity="info",
            attribution_scope="client",
            provider="openrouter",
            model="fixture/model",
            transport="http",
            response_status=200,
            status_kind="downstream_client_disconnect",
            error_code="ClientConnectionResetError",
            request_sha256="a" * 64,
        )
        tracker.observe_error(client_error)

        recovery = tracker.match_success(candidate())

        self.assertIsNone(recovery)

    def test_nearest_failure_is_correlated_and_consumed_once(self):
        tracker = RetryRecoveryTracker(window_seconds=10)
        tracker.observe_routing(routing("old", "ProviderOld"))
        tracker.observe_error(
            error(event_id="old", observed_at="2026-09-16T16:00:00+00:00")
        )
        tracker.observe_routing(routing("new", "ProviderA"))
        tracker.observe_error(
            error(event_id="new", observed_at="2026-09-16T16:00:01+00:00")
        )
        tracker.observe_routing(routing("retry", "ProviderB"))

        recovery = tracker.match_success(candidate())
        assert recovery is not None
        self.assertEqual(recovery.failed_event_id, "new")
        self.assertEqual(recovery.failed_selected_provider, "ProviderA")
        self.assertEqual(recovery.retry_selected_provider, "ProviderB")
        self.assertIs(recovery.provider_changed, True)

        tracker.commit_recovery(recovery)
        second = tracker.match_success(candidate(event_id="retry-2"))
        assert second is not None
        self.assertEqual(second.failed_event_id, "old")


if __name__ == "__main__":
    unittest.main()
