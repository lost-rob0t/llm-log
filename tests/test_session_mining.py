from __future__ import annotations

import json
import unittest

from llm_log.session_mining import mine_capture_failures, mine_history_events, replay_mined_failures


def capture(body=None, **overrides):
    event = {
        "event_id": "synthetic-request", "completed_at": "2026-10-31T00:00:00Z",
        "response_status": 200,
        "response_body": {"encoding": "utf-8", "text": json.dumps(body or {})},
        "history": {"session_sha256": "synthetic-session"},
    }
    return {**event, **overrides}


def values(result):
    return [(item["evidence_type"], item["observed_value"]) for item in result["payload"]["evidence"]]


class SessionMiningTests(unittest.TestCase):
    def test_final_attempt_failure_survives_metadata_retention(self):
        event = capture(response_body={"encoding": "omitted"}, attempts=[{"terminal_class": "midstream_failure"}])
        self.assertEqual(values(mine_capture_failures(event)), [("task_state", "aborted")])
        event["attempts"].append({"terminal_class": "success"})
        self.assertIsNone(mine_capture_failures(event))

    def test_prose_and_missing_fields_never_infer_failure(self):
        for body in ({}, {"content": [{"type": "text", "text": "error: test failed"}]},
                     {"status": "completed"}, {"choices": [{"finish_reason": "stop"}]}):
            with self.subTest(body=body):
                self.assertIsNone(mine_capture_failures(capture(body)))
        self.assertIsNone(mine_capture_failures(capture(response_body=None)))

    def test_http_failures_retain_numeric_weak_evidence(self):
        for status in (401, 403, 429, 500, 503, 599):
            result = mine_capture_failures(capture(response_status=status))
            self.assertEqual(values(result), [("provider_transport", status)])
            self.assertEqual(result["payload"]["evidence"][0]["authority"], "weak")
        for status in (200, 302, 400, 404, True, "500", None):
            self.assertIsNone(mine_capture_failures(capture(response_status=status)))

    def test_structured_provider_error_codes_and_provenance(self):
        result = mine_capture_failures(capture({"error": {"code": "insufficient_quota"}}))
        self.assertEqual(values(result), [("provider_transport", "quota_exceeded")])
        evidence = result["payload"]["evidence"][0]
        self.assertEqual(evidence["source_id"], "synthetic-request#response_body/error")
        self.assertEqual(evidence["observed_at"], "2026-10-31T00:00:00Z")
        self.assertEqual(result["session_id"], "synthetic-session")
        self.assertEqual(result["payload"]["scope"], "request")
        self.assertEqual(result, mine_capture_failures(capture({"error": {"code": "insufficient_quota"}})))

    def test_tool_failure_shapes(self):
        for tool in ({"type": "tool", "state": {"status": "error"}},
                     {"type": "tool_result", "is_error": True},
                     {"type": "tool", "state": {"error": "synthetic explicit error"}},
                     {"type": "function_call_output", "exit_code": 1},
                     {"type": "tool", "state": {"exit_code": -9}}):
            with self.subTest(tool=tool):
                result = mine_capture_failures(capture({"content": [tool]}))
                self.assertEqual(values(result), [("tool_result", "failure")])
        for code in (0, False, "1", None):
            self.assertIsNone(mine_capture_failures(capture({
                "content": [{"type": "tool", "state": {"exit_code": code}}]})))

    def test_two_explicit_tool_errors_keep_distinct_sources(self):
        tool = {"type": "tool", "state": {"status": "error"}}
        result = mine_capture_failures(capture({"content": [tool, tool]}))
        evidence = result["payload"]["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(len({row["source_id"] for row in evidence}), 2)
        self.assertEqual(len({row["evidence_id"] for row in evidence}), 2)

    def test_explicit_response_end_states(self):
        for body, expected in (
            ({"choices": [{"finish_reason": "length"}]}, "truncated"),
            ({"stop_reason": "max_tokens"}, "truncated"),
            ({"finish": "length"}, "truncated"),
            ({"status": "aborted"}, "aborted"),
            ({"response": {"status": "cancelled"}}, "cancelled"),
            ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "truncated"),
        ):
            self.assertEqual(values(mine_capture_failures(capture(body))), [("task_state", expected)])
        self.assertIsNone(mine_capture_failures(capture({"status": "incomplete"})))

    def test_sse_and_websocket_frames(self):
        for text in (
            'event: message_delta\ndata: {"delta":{"stop_reason":"max_tokens"}}\n\ndata: [DONE]\n',
            json.dumps({"type": "text", "text": json.dumps({"status": "aborted"})}) + '\n' + '{}',
        ):
            result = mine_capture_failures(capture(response_body={"encoding": "utf-8", "text": text}))
            self.assertIsNotNone(result)
            self.assertIn("/line/", result["payload"]["evidence"][0]["source_id"])

    def test_single_websocket_frame_is_unwrapped(self):
        event = capture({"type": "text", "text": json.dumps({"status": "aborted"})})
        result = mine_capture_failures(event)
        self.assertEqual(values(result), [("task_state", "aborted")])
        self.assertEqual(result["payload"]["evidence"][0]["source_id"],
                         "synthetic-request#response_body/text/status")

    def test_expert_evidence_bound_is_explicit(self):
        body = {"content": [{"type": "tool_result", "is_error": True} for _ in range(65)]}
        with self.assertRaisesRegex(ValueError, "64"):
            mine_capture_failures(capture(body))

    def test_iterable_contract_omits_unknowns(self):
        self.assertEqual(len(mine_history_events(iter([capture(), capture(response_status=429)]))), 1)


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_expert_ingestion_contract_only(self):
        class Plane:
            def __init__(self):
                self.calls = []

            async def record_outcome_evidence(self, **kwargs):
                self.calls.append(kwargs)
                return {"outcome": "failure", "rule_id": "synthetic-rule"}

        plane = Plane()
        event = capture(response_status=401)
        result = await replay_mined_failures(plane, event)
        self.assertEqual(result["outcome"], "failure")
        self.assertEqual(plane.calls, [mine_capture_failures(event)])
        self.assertIsNone(await replay_mined_failures(plane, capture()))
        self.assertEqual(len(plane.calls), 1)
