import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class RecordingExpert:
    def __init__(self):
        self.calls = []

    async def health(self):
        return {"ok": True}

    async def observe_request(self, *, event_id, payload, session_id, task_id):
        self.calls.append(("request", event_id, dict(payload)))
        return {"projection_state": "created"}

    async def classify_request(self, *, event_id, payload, session_id, task_id):
        self.calls.append(("classify", event_id, dict(payload)))
        return {"assertions": []}

    async def observe_response(self, *, event_id, payload, session_id, task_id):
        self.calls.append(("response", event_id, dict(payload)))
        return {"projection_state": "created"}

    async def observe_usage(self, *, event_id, payload, session_id, task_id):
        self.calls.append(("usage", event_id, dict(payload)))
        return {"projection_state": "created"}


class ExpertResponseIngestTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expert = RecordingExpert()

        upstream = web.Application()
        upstream.router.add_post("/v1/stream", self.stream)
        upstream.router.add_post("/v1/no-usage", self.no_usage)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        self.recorder = RecorderActor(self.root)
        self.proxy = build_app(
            {"test": f"http://127.0.0.1:{upstream_port}"},
            self.recorder,
            classifier=None,
            expert_plane=self.expert,
        )
        self.proxy_runner = web.AppRunner(self.proxy)
        await self.proxy_runner.setup()
        proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def stream(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":5}}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def no_usage(self, request):
        await request.read()
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def _post(self, path):
        async with ClientSession() as session:
            async with session.post(
                f"{self.proxy_url}/test{path}",
                json={"model": "fixture/model", "messages": [{"role": "user", "content": "hello"}]},
            ) as response:
                await response.read()
        await self.recorder.flush()
        await asyncio.sleep(0.05)

    async def test_response_and_usage_are_observed_once_with_safe_metadata(self):
        await self._post("/v1/stream")

        responses = [call for call in self.expert.calls if call[0] == "response"]
        usages = [call for call in self.expert.calls if call[0] == "usage"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(len(usages), 1)
        self.assertEqual(responses[0][1], usages[0][1])

        response = responses[0][2]
        self.assertEqual(response["response_status"], 200)
        self.assertEqual(response["finish_reason"], "stop")
        self.assertIs(response["stream_completed"], True)
        self.assertIn("response_sha256", response)
        self.assertNotIn("response_body", response)

        usage = usages[0][2]
        self.assertEqual(usage["request_id"], usages[0][1])
        self.assertEqual(usage["input_tokens"], 12)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertNotIn("response_body", usage)

    async def test_missing_usage_does_not_emit_usage_observation(self):
        await self._post("/v1/no-usage")

        responses = [call for call in self.expert.calls if call[0] == "response"]
        usages = [call for call in self.expert.calls if call[0] == "usage"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(usages, [])


if __name__ == "__main__":
    unittest.main()
