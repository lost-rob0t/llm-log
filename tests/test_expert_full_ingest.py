from __future__ import annotations

import asyncio
import hashlib
import json
import unittest

from llm_log.expert_capture import project_capture_event


class FakeExpertPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def _record(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        return {"projection_state": "created"}

    async def observe_request(self, **kwargs):
        return await self._record("observe_request", **kwargs)

    async def observe_user_message(self, **kwargs):
        return await self._record("observe_user_message", **kwargs)

    async def classify_request(self, **kwargs):
        return await self._record("query_classification", **kwargs)

    async def observe_response(self, **kwargs):
        return await self._record("observe_response", **kwargs)

    async def observe_usage(self, **kwargs):
        return await self._record("observe_usage", **kwargs)


def captured(raw: bytes) -> dict[str, str]:
    return {"encoding": "utf-8", "text": raw.decode("utf-8")}


class ExpertFullIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_projects_full_bounded_capture_context_with_attribution(self) -> None:
        request = json.dumps(
            {
                "model": "openai/gpt-5.6-sol",
                "messages": [
                    {"role": "user", "content": "Debug the failing worker and test the fix."},
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": "fixture result",
                    },
                ],
            }
        ).encode()
        response = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call-2",
                                    "type": "function",
                                    "function": {"name": "search_code", "arguments": "{}"},
                                }
                            ]
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            }
        ).encode()
        event = {
            "event_id": "event-1",
            "provider": "openrouter",
            "upstream": "https://openrouter.ai",
            "method": "POST",
            "path": "/api/v1/chat/completions",
            "query": "",
            "request_body": captured(request),
            "response_body": captured(response),
            "response_status": 200,
            "started_at": "2026-09-20T20:00:00+00:00",
            "completed_at": "2026-09-20T20:00:01+00:00",
            "latency_ms": 875,
            "model": "openai/gpt-5.6-sol",
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "transport": "http",
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "attribution": {
                "session": "session-9",
                "task": "task-7",
                "agent": "opencode",
                "correlation_id": "corr-1",
                "causation_id": "cause-1",
                "plan": "plan-2",
            },
        }

        plane = FakeExpertPlane()
        result = await project_capture_event(plane, event)

        self.assertEqual(
            [name for name, _ in plane.calls],
            [
                "observe_request",
                "observe_user_message",
                "query_classification",
                "observe_response",
                "observe_usage",
            ],
        )
        for _, call in plane.calls:
            self.assertEqual(call["session_id"], "session-9")
            self.assertEqual(call["task_id"], "task-7")

        request_payload = plane.calls[0][1]["payload"]
        self.assertEqual(request_payload["upstream"], "https://openrouter.ai")
        self.assertEqual(request_payload["method"], "POST")
        self.assertEqual(request_payload["client"], "opencode")
        self.assertEqual(request_payload["correlation_id"], "corr-1")
        self.assertEqual(request_payload["tool_result_count"], 1)

        message_payload = plane.calls[1][1]["payload"]
        self.assertIn("Debug the failing worker", message_payload["message"])
        self.assertEqual(message_payload["client"], "opencode")

        response_payload = plane.calls[3][1]["payload"]
        self.assertEqual(response_payload["response_status"], 200)
        self.assertEqual(response_payload["latency_ms"], 875)
        self.assertEqual(response_payload["finish_reasons"], ["tool_calls"])
        self.assertEqual(response_payload["tool_call_names"], ["search_code"])
        self.assertEqual(response_payload["tool_call_count"], 1)
        self.assertEqual(response_payload["total_tokens"], 150)

        self.assertEqual(result["event_id"], "event-1")
        self.assertIsNotNone(result["response"])
        self.assertIsNotNone(result["usage"])

    async def test_missing_usage_stays_missing(self) -> None:
        request = b'{"model":"fixture","messages":[{"role":"user","content":"hello"}]}'
        response = b'{"choices":[{"finish_reason":"stop","message":{"content":"hi"}}]}'
        event = {
            "event_id": "event-2",
            "provider": "fixture",
            "upstream": "https://fixture.invalid",
            "method": "POST",
            "path": "/v1/chat/completions",
            "query": "",
            "request_body": captured(request),
            "response_body": captured(response),
            "response_status": 200,
            "started_at": "2026-09-20T20:00:00+00:00",
            "completed_at": "2026-09-20T20:00:01+00:00",
            "latency_ms": 100,
            "model": "fixture",
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "transport": "http",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "attribution": {},
        }
        plane = FakeExpertPlane()
        result = await project_capture_event(plane, event)
        self.assertNotIn("observe_usage", [name for name, _ in plane.calls])
        self.assertIsNone(result["usage"])


if __name__ == "__main__":
    unittest.main()
