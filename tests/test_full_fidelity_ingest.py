from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class FullRecordingExpertPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, str, str]] = []

    async def health(self) -> dict:
        return {}

    def _record(self, operation: str, *, event_id, payload, session_id, task_id):
        self.calls.append((operation, event_id, dict(payload), session_id, task_id))
        return {"operation": operation}

    async def observe_request(self, **kwargs):
        return self._record("observe_request", **kwargs)

    async def observe_user_message(self, **kwargs):
        return self._record("observe_user_message", **kwargs)

    async def classify_request(self, **kwargs):
        return self._record("classify_request", **kwargs)

    async def observe_response(self, **kwargs):
        return self._record("observe_response", **kwargs)

    async def assess_response(self, **kwargs):
        return self._record("assess_response", **kwargs)

    async def observe_usage(self, **kwargs):
        return self._record("observe_usage", **kwargs)

    async def record_outcome_evidence(self, **kwargs):
        return self._record("record_outcome_evidence", **kwargs)


class FullFidelityExpertIngestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        upstream = web.Application()
        upstream.router.add_post("/v1/chat/completions", self._upstream)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def _upstream(self, request):
        await request.read()
        return web.json_response(
            {
                "id": "fixture-response",
                "model": "fixture-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }
        )

    async def _start_proxy(self, expert_plane):
        recorder = RecorderActor(self.root)
        app = build_app(
            {"fixture": self.upstream_url},
            recorder,
            classifier=None,
            expert_plane=expert_plane,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(runner.cleanup)
        return f"http://127.0.0.1:{port}"

    async def test_live_capture_projects_full_bounded_context(self):
        plane = FullRecordingExpertPlane()
        proxy_url = await self._start_proxy(plane)
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/fixture/v1/chat/completions",
                json={
                    "model": "fixture-model",
                    "messages": [
                        {"role": "user", "content": "fix the parser and run tests"}
                    ],
                },
                headers={
                    "X-LLM-Log-Agent": "gptel",
                    "X-LLM-Log-Session": "session-42",
                    "X-LLM-Log-Task": "task-99",
                    "X-LLM-Log-Correlation-ID": "corr-abc",
                    "X-LLM-Log-Causation-ID": "cause-def",
                },
            ) as response:
                self.assertEqual(response.status, 200)
                await response.read()

        expected = [
            "observe_request",
            "observe_user_message",
            "classify_request",
            "observe_response",
            "assess_response",
            "observe_usage",
            "record_outcome_evidence",
        ]
        for _ in range(150):
            if len(plane.calls) >= len(expected):
                break
            await asyncio.sleep(0.02)

        self.assertEqual([call[0] for call in plane.calls], expected)

        request = plane.calls[0]
        request_id = request[1]
        payload = request[2]
        self.assertEqual(request[3], "session-42")
        self.assertEqual(request[4], "task-99")
        self.assertEqual(payload["provider"], "fixture")
        self.assertEqual(payload["upstream"], self.upstream_url)
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["path"], "/v1/chat/completions")
        self.assertEqual(payload["attribution"]["agent"], "gptel")
        self.assertEqual(payload["attribution"]["correlation_id"], "corr-abc")
        self.assertEqual(payload["attribution"]["causation_id"], "cause-def")
        self.assertNotIn("request_body", payload)
        self.assertNotIn("response_body", payload)

        message = plane.calls[1]
        self.assertEqual(message[2]["request_id"], request_id)
        self.assertEqual(message[2]["message"], "fix the parser and run tests")
        self.assertEqual(message[2]["message_truncated"], False)
        self.assertEqual(len(message[2]["message_sha256"]), 64)

        response = plane.calls[3]
        self.assertEqual(response[2]["request_id"], request_id)
        self.assertEqual(response[2]["response_status"], 200)
        self.assertEqual(response[2]["finish_reason"], "stop")
        self.assertEqual(len(response[2]["response_sha256"]), 64)
        self.assertNotIn("body", response[2])

        assessment = plane.calls[4]
        self.assertEqual(assessment[2]["request_id"], request_id)
        self.assertEqual(assessment[2]["response_status"], 200)

        usage = plane.calls[5]
        self.assertEqual(usage[1], request_id)
        self.assertEqual(usage[2]["request_id"], request_id)
        self.assertEqual(usage[2]["input_tokens"], 12)
        self.assertEqual(usage[2]["output_tokens"], 4)

        outcome = plane.calls[6]
        self.assertEqual(outcome[2]["scope_id"], request_id)
        evidence = outcome[2]["evidence"][0]
        self.assertEqual(evidence["authority"], "weak")
        self.assertEqual(evidence["evidence_type"], "provider_transport")
        self.assertEqual(evidence["observed_value"], 200)


if __name__ == "__main__":
    unittest.main()
