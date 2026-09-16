import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from llm_log.recovery import RetryRecoveryEvidence
from llm_log.transport_errors import TransportErrorEvidence


def alert_api():
    return importlib.import_module("llm_log.alert_policy")


def error(
    error_class,
    *,
    event_id="evt-1",
    attribution_scope="provider_or_gateway",
    response_status=502,
    model="fixture/model",
):
    return TransportErrorEvidence(
        error_id=f"transport:{event_id}:{error_class}",
        event_id=event_id,
        observed_at="2026-09-16T16:00:00Z",
        error_class=error_class,
        domain="transport",
        severity="warning",
        attribution_scope=attribution_scope,
        provider="openrouter",
        model=model,
        transport="http",
        response_status=response_status,
        status_kind="upstream",
        error_code=502,
        request_sha256="a" * 64,
    )


def recovery():
    return RetryRecoveryEvidence(
        recovery_id="recovery:evt-1:evt-2",
        observed_at="2026-09-16T16:00:03Z",
        outcome="recovered",
        failed_event_id="evt-1",
        retry_event_id="evt-2",
        request_sha256="a" * 64,
        failed_error_class="upstream_connection_reset",
        retry_delay_ms=3000,
        failed_selected_provider="ProviderA",
        retry_selected_provider="ProviderB",
        provider_changed=True,
    )


class AlertPolicyContractTest(unittest.TestCase):
    def test_client_disconnect_is_log_only_and_never_provider_alert(self):
        api = alert_api()
        decision = api.decision_for_error(
            error(
                "downstream_client_disconnect",
                attribution_scope="client",
                response_status=200,
            )
        )
        self.assertEqual(decision.action, "log_only")
        self.assertEqual(decision.severity, "info")
        self.assertEqual(decision.rate_class, "none")
        self.assertEqual(decision.grace_seconds, 0)
        self.assertEqual(decision.max_notifications_per_window, 0)

    def test_account_gateway_failure_alerts_immediately_but_is_rate_limited(self):
        api = alert_api()
        decision = api.decision_for_error(
            error(
                "upstream_http_error",
                attribution_scope="gateway_or_account",
                response_status=403,
                model=None,
            )
        )
        self.assertEqual(decision.action, "alert")
        self.assertEqual(decision.severity, "warning")
        self.assertEqual(decision.rate_class, "account_gateway")
        self.assertEqual(decision.grace_seconds, 0)
        self.assertGreater(decision.rate_window_seconds, 60)
        self.assertEqual(decision.max_notifications_per_window, 1)
        self.assertIsNone(decision.model)

    def test_transient_provider_failure_defers_during_recovery_grace(self):
        api = alert_api()
        decision = api.decision_for_error(error("upstream_connection_reset"))
        self.assertEqual(decision.action, "alert_if_unrecovered")
        self.assertEqual(decision.severity, "warning")
        self.assertEqual(decision.rate_class, "provider_transient")
        self.assertEqual(decision.grace_seconds, 10)
        self.assertEqual(decision.max_notifications_per_window, 1)

    def test_protocol_integrity_has_a_distinct_rate_policy(self):
        api = alert_api()
        transient = api.decision_for_error(error("upstream_connection_reset"))
        protocol = api.decision_for_error(error("stream_protocol_error", event_id="evt-3"))
        self.assertEqual(protocol.action, "alert_if_unrecovered")
        self.assertEqual(protocol.rate_class, "protocol_integrity")
        self.assertGreater(
            protocol.rate_window_seconds,
            transient.rate_window_seconds,
        )

    def test_recovery_emits_explicit_suppression_decision(self):
        api = alert_api()
        decision = api.decision_for_recovery(recovery())
        self.assertEqual(decision.action, "suppress_recovered")
        self.assertEqual(decision.severity, "info")
        self.assertEqual(decision.event_id, "evt-1")
        self.assertEqual(decision.recovery_id, "recovery:evt-1:evt-2")
        self.assertEqual(decision.reason, "recovered_within_retry_correlation")


class AlertPolicyActorContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_actor_persists_safe_decisions_in_order(self):
        api = alert_api()
        with tempfile.TemporaryDirectory() as tmp:
            actor = api.AlertPolicyActor(Path(tmp))
            await actor.start()
            first = await actor.observe_error(error("upstream_connection_reset"))
            second = await actor.observe_recovery(recovery())
            await actor.close()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "alert-decisions.jsonl").read_text().splitlines()
                if line
            ]
            self.assertEqual(
                [record["decision_id"] for record in records],
                [first.decision_id, second.decision_id],
            )
            self.assertNotIn("request_sha256", json.dumps(records))
            self.assertNotIn("ProviderA", json.dumps(records))
            self.assertNotIn("ProviderB", json.dumps(records))


if __name__ == "__main__":
    unittest.main()
