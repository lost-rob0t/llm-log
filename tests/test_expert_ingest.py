from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.expert_adapter import ExpertAdapterError
from llm_log.proxy import _extract_user_message, build_app
from llm_log.recorder import RecorderActor


class RecordingExpertPlane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, str, str]] = []

    async def health(self) -> dict:
        return {}

    async def observe_request(self, *, event_id, payload, session_id, task_id) -> dict:
        self.calls.append(("observe_request", event_id, dict(payload), session_id, task_id))
        return {"projection_state": "created", "kb_revision": 2}

    async def classify_request(self, *, event_id, payload, session_id, task_id) -> dict:
        self.calls.append(("classify_request", event_id, dict(payload), session_id, task_id))
        return {"expert": "request.classifier", "assertions": []}


class BoomExpertPlane:
    async def health(self) -> dict:
        return {}

    async def observe_request(self, **kwargs) -> dict:
        raise ExpertAdapterError("expert service timed out")

    async def classify_request(self, **kwargs) -> dict:
        raise AssertionError("classification must not run after a failed observe")


class UserMessageExtractionTest(unittest.TestCase):
    def test_openai_style_string_content(self):
        body = json.dumps({"messages": [{"role": "user", "content": "fix the proxy"}]})
        self.assertEqual(_extract_user_message(body.encode()), "fix the proxy")

    def test_anthropic_style_part_content(self):
        body = json.dumps({"messages": [
            {"role": "user", "content": [{"type": "text", "text": "add tests"}]},
        ]})
        self.assertEqual(_extract_user_message(body.encode()), "add tests")

    def test_prefers_the_last_user_message(self):
        body = json.dumps({"messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "nope"},
            {"role": "user", "content": "second"},
        ]})
        self.assertEqual(_extract_user_message(body.encode()), "second")

    def test_websocket_envelope_is_unwrapped(self):
        frame = json.dumps({"type": "text", "text": json.dumps(
            {"messages": [{"role": "user", "content": "ws message"}]})})
        self.assertEqual(_extract_user_message((frame + "\n").encode()), "ws message")

    def test_non_json_body_yields_empty_message(self):
        self.assertEqual(_extract_user_message(b"not json at all"), "")


class ExpertIngestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        upstream = web.Application()
        upstream.router.add_post("/v1/messages", self._upstream)
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
        return web.json_response({"ok": True})

    async def _start_proxy(self, expert_plane):
        recorder = RecorderActor(self.root)
        app = build_app({"test": self.upstream_url}, recorder, classifier=None,
                        expert_plane=expert_plane)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(runner.cleanup)
        return f"http://127.0.0.1:{port}"

    async def test_every_capture_is_observed_and_classified(self):
        plane = RecordingExpertPlane()
        proxy_url = await self._start_proxy(plane)
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/test/v1/messages",
                json={"model": "fixture", "messages": [{"role": "user", "content": "fix the classifier"}]},
            ) as response:
                self.assertEqual(response.status, 200)
        for _ in range(100):
            if len(plane.calls) >= 2:
                break
            await asyncio.sleep(0.02)

        self.assertEqual([c[0] for c in plane.calls], ["observe_request", "classify_request"])
        operation, event_id, payload, session_id, task_id = plane.calls[0]
        self.assertEqual(operation, "observe_request")
        self.assertEqual(session_id, "proxy")
        self.assertEqual(task_id, "proxy")
        self.assertEqual(payload["provider"], "test")
        self.assertEqual(payload["model"], "fixture")
        self.assertEqual(payload["transport"], "http")
        self.assertEqual(len(payload["request_sha256"]), 64)
        self.assertEqual(len(payload["response_sha256"]), 64)

        operation, event_id, payload, _, _ = plane.calls[1]
        self.assertEqual(operation, "classify_request")
        self.assertEqual(event_id, plane.calls[0][1], "classify must reuse the capture event id")
        self.assertEqual(payload["message"], "fix the classifier")
        self.assertEqual(payload["request_id"], event_id)
        self.assertTrue(payload["user_message_id"].startswith("um-"))
        self.assertEqual(payload["client"], "proxy")

    async def test_failed_expert_never_breaks_the_proxy(self):
        plane = BoomExpertPlane()
        proxy_url = await self._start_proxy(plane)
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/test/v1/messages",
                json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            ) as response:
                self.assertEqual(response.status, 200)
        await asyncio.sleep(0.05)

    async def test_keywordless_bodies_observe_without_classification(self):
        plane = RecordingExpertPlane()
        proxy_url = await self._start_proxy(plane)
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/test/v1/messages",
                data=b"not-json-body",
                headers={"Content-Type": "text/plain"},
            ) as response:
                self.assertEqual(response.status, 200)
        for _ in range(100):
            if len(plane.calls) >= 1:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        self.assertEqual([c[0] for c in plane.calls], ["observe_request"])


if __name__ == "__main__":
    unittest.main()
