"""Synthetic tests for request-scoped SWI-Prolog failure decisions."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("swipl"), "SWI-Prolog unavailable")
class FailureRuleTests(unittest.TestCase):
    def decide(self, evidence, scope="request"):
        worker = Path(__file__).parents[1] / "expert/prolog/worker.pl"
        request = {"version": 1, "request_id": "synthetic", "operation": "outcome_decision",
                   "data": {"scope": scope, "scope_id": "synthetic", "evidence": evidence}}
        result = subprocess.run([shutil.which("swipl"), "-q", "-f", str(worker)],
                                input=json.dumps(request) + '\n', capture_output=True,
                                text=True, timeout=5, check=True)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["status"], "ok", reply)
        self.assertEqual(reply["rule_version"], "outcome.decision/v2")
        self.assertEqual(reply["result"]["evidence_ids"], [e["evidence_id"] for e in evidence])
        return reply["result"]

    def item(self, kind, value, authority="normal", source="synthetic-1"):
        return {"evidence_id": source, "source_id": source, "observed_at": "2026-10-31T00:00:00Z",
                "evidence_type": kind, "authority": authority, "observed_value": value}

    def test_provider_failure_rules(self):
        for value, rule in ((401, "authentication_error"), (403, "access_denied"),
                            (429, "rate_or_quota_limit"), (503, "server_error"),
                            ("quota_exceeded", "quota_exhausted"),
                            ("rate_limit_exceeded", "rate_limit"),
                            ("error", "error")):
            with self.subTest(value=value):
                result = self.decide([self.item("provider_transport", value)])
                self.assertEqual(result["outcome"], "failure")
                self.assertEqual(result["rule_id"], "outcome.provider_" + rule)

    def test_observed_tool_test_and_end_states(self):
        for kind, value, outcome, rule in (
            ("tool_result", "failure", "failure", "observed_tool_failure"),
            ("test_result", "failure", "failure", "observed_test_failure"),
            ("task_state", "truncated", "partial", "response_truncated"),
            ("task_state", "aborted", "partial", "response_aborted"),
            ("task_state", "timeout", "timeout", "observed_timeout"),
            ("task_state", "cancelled", "cancelled", "observed_cancelled"),
        ):
            result = self.decide([self.item(kind, value)])
            self.assertEqual(result["outcome"], outcome)
            self.assertEqual(result["rule_id"], "outcome." + rule)

    def test_mined_capture_evidence_reaches_reasoner_unchanged(self):
        from llm_log.session_mining import mine_capture_failures
        event = {
            "event_id": "synthetic", "completed_at": "2026-10-31T00:00:00Z",
            "response_status": 429,
            "response_body": {"encoding": "utf-8", "text": json.dumps({
                "error": {"code": "insufficient_quota"}})},
        }
        envelope = mine_capture_failures(event)
        result = self.decide(envelope["payload"]["evidence"])
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(result["rule_id"], "outcome.provider_quota_exhausted")

    def test_repeated_error_requires_distinct_sources(self):
        first = self.item("tool_result", "failure")
        second = self.item("tool_result", "failure", source="synthetic-2")
        result = self.decide([first, second])
        self.assertEqual(result["rule_id"], "outcome.repeated_tool_error")
        second["source_id"] = first["source_id"]
        result = self.decide([first, second])
        self.assertEqual(result["rule_id"], "outcome.observed_tool_failure")

    def test_authoritative_outcomes_keep_precedence(self):
        observations = [self.item("provider_transport", 503)]
        for value in ("success", "failure"):
            evidence = self.item("manual_label", value, "authoritative", "label")
            result = self.decide(observations + [evidence])
            self.assertEqual(result["outcome"], value)
            self.assertEqual(result["rule_id"], "outcome.authoritative_" + value)

    def test_unknown_and_task_boundaries(self):
        for kind, value, authority, scope in (
            ("provider_transport", 200, "weak", "request"),
            ("provider_transport", 403, "weak", "task"),
            ("tool_result", "failure", "normal", "task"),
            ("tool_result", "failure", "weak", "request"),
            ("tool_result", "success", "normal", "request"),
            ("task_state", "incomplete", "normal", "request"),
        ):
            result = self.decide([self.item(kind, value, authority)], scope)
            self.assertEqual(result["outcome"], "unknown")
            self.assertEqual(result["rule_id"], "outcome.insufficient_evidence")
