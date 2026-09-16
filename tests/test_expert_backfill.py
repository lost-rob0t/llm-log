from __future__ import annotations

import base64
import json
import unittest

from llm_log.expert_capture import (
    decode_captured_body,
    expert_response_payload,
    expert_usage_payload,
    replay_capture_event,
)


class RecordingPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def observe_request(self, *, event_id, payload, **_kwargs):
        self.calls.append(("observe_request", event_id, dict(payload)))
        return {"projection_state": "created"}

    async def classify_request(self, *, event_id, payload, **_kwargs):
        self.calls.append(("classify_request", event_id, dict(payload)))
        return {"assertions": []}

    async def observe_response(self, *, event_id, payload, **_kwargs):
        self.calls.append(("observe_response", event_id, dict(payload)))
        return {"projection_state": "created", "response_id": event_id}

    async def observe_usage(self, *, event_id, payload, **_kwargs):
        self.calls.append(("observe_usage", event_id, dict(payload)))
        return {"projection_state": "created", "usage_id": payload["usage_id"]}

    async def record_outcome_evidence(self, *, event_id, payload, **_kwargs):
        self.calls.append(("record_outcome_evidence", event_id, dict(payload)))
        return {"outcome": "unknown"}


def capture_event() -> dict:
    request = json.dumps(
        {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "fix the classifier"}],
        }
    )
    return {
        "event_id": "12345678-aaaa-bbbb-cccc-000000000000",
        "provider": "openrouter",
        "upstream": "https://openrouter.ai",
        "request_body": {"encoding": "utf-8", "text": request},
        "response_status": 200,
        "started_at": "2026-09-14T01:00:00Z",
        "completed_at": "2026-09-14T01:00:01Z",
        "latency_ms": 1000,
        "model": "fixture/model",
        "transport": "http",
        "status_kind": "upstream",
        "stream_completed": None,
        "finish_reason": None,
        "request_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "input_tokens": 120,
        "output_tokens": 30,
    }


class CaptureDecodeTests(unittest.TestCase):
    def test_utf8_and_base64(self):
        self.assertEqual(decode_captured_body({"encoding": "utf-8", "text": "hi"}), b"hi")
        self.assertEqual(
            decode_captured_body(
                {"encoding": "base64", "data": base64.b64encode(b"bytes").decode()}
            ),
            b"bytes",
        )

    def test_response_payload_is_safe_and_preserves_stream_state(self):
        event = capture_event()
        event["stream_completed"] = False
        response = expert_response_payload(event)
        self.assertEqual(response["request_id"], event["event_id"])
        self.assertEqual(response["stream_state"], "incomplete")
        self.assertNotIn("response_body", response)

    def test_usage_keeps_raw_request_identity_without_inventing_task(self):
        usage = expert_usage_payload(capture_event())
        assert usage is not None
        self.assertEqual(usage["request_id"], capture_event()["event_id"])
        self.assertEqual(usage["usage_id"], "capture-usage:" + capture_event()["event_id"])
        self.assertNotIn("task_id", usage)


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replays_request_classification_response_and_usage_with_stable_ids(self):
        plane = RecordingPlane()
        event = capture_event()
        result = await replay_capture_event(plane, event)

        self.assertEqual(
            [call[0] for call in plane.calls],
            ["observe_request", "classify_request", "observe_response", "observe_usage"],
        )
        classification = plane.calls[1][2]
        self.assertEqual(classification["request_id"], event["event_id"])
        self.assertEqual(classification["user_message_id"], "um-12345678")
        response = plane.calls[2][2]
        self.assertEqual(response["request_id"], event["event_id"])
        self.assertEqual(response["response_sha256"], event["response_sha256"])
        self.assertEqual(result["event_id"], event["event_id"])
        self.assertIsNone(result["transport_outcome"])

    async def test_transport_evidence_is_explicit_and_weak(self):
        plane = RecordingPlane()
        event = capture_event()
        await replay_capture_event(plane, event, record_transport_evidence=True)
        operation, event_id, payload = plane.calls[-1]
        self.assertEqual(operation, "record_outcome_evidence")
        self.assertEqual(event_id, "capture-transport:" + event["event_id"])
        evidence = payload["evidence"][0]
        self.assertEqual(evidence["authority"], "weak")
        self.assertEqual(evidence["evidence_type"], "provider_transport")
        self.assertEqual(evidence["observed_value"], 200)


if __name__ == "__main__":
    unittest.main()
