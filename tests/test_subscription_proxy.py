from __future__ import annotations

import json
import unittest

from llm_log.cli import _DEFAULT_UPSTREAMS
from llm_log.proxy import _request_headers
from llm_log.recorder import CaptureEvent


class SubscriptionProxyTests(unittest.TestCase):
    def test_subscription_routes_are_builtin(self) -> None:
        self.assertEqual(_DEFAULT_UPSTREAMS["chatgpt"], "https://chatgpt.com")
        self.assertEqual(_DEFAULT_UPSTREAMS["zai-coding"], "https://api.z.ai")

    def test_internal_attribution_headers_are_recorded_but_not_forwarded(self) -> None:
        headers = {
            "Authorization": "Bearer subscription-secret",
            "X-LLM-Log-Company": "starintel_labs",
            "X-LLM-Log-Worker": "opencode-16",
            "X-LLM-Log-Agent": "builder",
            "X-LLM-Log-Session": "ses_parent",
            "X-LLM-Log-Task": "task-42",
            "Content-Type": "application/json",
        }
        forwarded = _request_headers(headers)
        self.assertIn("Authorization", forwarded)
        self.assertNotIn("X-LLM-Log-Worker", forwarded)
        self.assertNotIn("X-LLM-Log-Agent", forwarded)
        self.assertNotIn("X-LLM-Log-Session", forwarded)

        event = CaptureEvent.from_bytes(
            event_id="evt-1",
            provider="zai-coding",
            upstream="https://api.z.ai",
            method="POST",
            path="/api/coding/paas/v4/chat/completions",
            query="",
            request_headers=headers,
            request_body=json.dumps({"model": "glm-test"}).encode(),
            response_status=200,
            response_headers={"Content-Type": "application/json"},
            response_body=json.dumps({
                "usage": {
                    "prompt_tokens": 123,
                    "completion_tokens": 45,
                    "total_tokens": 168,
                }
            }).encode(),
            started_at="2026-09-19T00:00:00+00:00",
            completed_at="2026-09-19T00:00:01+00:00",
            latency_ms=1000,
        )

        self.assertEqual(event.input_tokens, 123)
        self.assertEqual(event.output_tokens, 45)
        self.assertEqual(event.total_tokens, 168)
        self.assertEqual(event.attribution["worker"], "opencode-16")
        self.assertEqual(event.attribution["agent"], "builder")
        self.assertEqual(event.attribution["session"], "ses_parent")
        self.assertEqual(event.attribution["task"], "task-42")
        self.assertEqual(event.request_headers["Authorization"], "<redacted>")

    def test_subagent_attribution_is_first_class(self) -> None:
        event = CaptureEvent.from_bytes(
            event_id="evt-child",
            provider="chatgpt",
            upstream="https://chatgpt.com",
            method="POST",
            path="/backend-api/codex/responses",
            query="",
            request_headers={
                "X-LLM-Log-Worker": "opencode-16",
                "X-LLM-Log-Agent": "reviewer-critic",
                "X-LLM-Log-Session": "ses_child",
            },
            request_body=b'{"model":"gpt-test"}',
            response_status=200,
            response_headers={},
            response_body=b'{"usage":{"input_tokens":10,"output_tokens":2}}',
            started_at="2026-09-19T00:00:00+00:00",
            completed_at="2026-09-19T00:00:01+00:00",
            latency_ms=1000,
        )
        self.assertEqual(
            event.attribution,
            {
                "worker": "opencode-16",
                "agent": "reviewer-critic",
                "session": "ses_child",
            },
        )
        self.assertIn("llm_attribution('evt-child', agent, 'reviewer-critic').", event.as_prolog())


if __name__ == "__main__":
    unittest.main()
