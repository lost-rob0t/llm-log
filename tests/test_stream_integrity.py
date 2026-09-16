import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class StreamIntegrityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        upstream = web.Application()
        upstream.router.add_post("/v1/truncated", self.truncated_stream)
        upstream.router.add_post("/v1/completed", self.completed_stream)
        upstream.router.add_post("/v1/abrupt", self.abrupt_stream)
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

    async def truncated_stream(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{"content":"half"},"finish_reason":null}]}\n\n'
        )
        await response.write_eof()
        return response

    async def completed_stream(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def abrupt_stream(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"choices":[{"delta":{"content":"one"},"finish_reason":null}]}\n\n'
        )
        transport = request.transport
        self.assertIsNotNone(transport)
        transport.abort()
        return response

    async def _post(self, path):
        async with ClientSession() as session:
            async with session.post(
                f"{self.proxy_url}/test{path}",
                json={"model": "stream-model", "messages": [{"role": "user", "content": "go"}]},
            ) as response:
                return response.status, await response.read()

    async def _event(self):
        await self.recorder.flush()
        lines = (self.root / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    async def test_clean_eof_without_terminal_marker_is_incomplete(self):
        status, body = await self._post("/v1/truncated")

        self.assertEqual(status, 200)
        self.assertIn(b'"content":"half"', body)
        event = await self._event()
        self.assertEqual(event["response_status"], 200)
        self.assertEqual(event["status_kind"], "upstream")
        self.assertIs(event["stream_completed"], False)
        self.assertIsNone(event["finish_reason"])

    async def test_terminal_sse_records_completion_and_finish_reason(self):
        status, body = await self._post("/v1/completed")

        self.assertEqual(status, 200)
        self.assertIn(b"data: [DONE]", body)
        event = await self._event()
        self.assertEqual(event["status_kind"], "upstream")
        self.assertIs(event["stream_completed"], True)
        self.assertEqual(event["finish_reason"], "stop")

    async def test_midstream_upstream_abort_keeps_prepared_response_and_records_failure(self):
        status, body = await self._post("/v1/abrupt")

        self.assertEqual(status, 200)
        self.assertIn(b'"content":"one"', body)
        self.assertNotIn(b"upstream request failed", body)
        event = await self._event()
        self.assertEqual(event["response_status"], 200)
        self.assertEqual(event["status_kind"], "upstream_midstream_error")
        self.assertIs(event["stream_completed"], False)
        self.assertIsNone(event["finish_reason"])


if __name__ == "__main__":
    unittest.main()
