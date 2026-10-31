from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class SubscriptionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream_requests: list[dict] = []
        self.upstream = web.Application()
        self.upstream.router.add_route("*", "/{tail:.*}", self._upstream)
        self.upstream_runner = web.AppRunner(self.upstream)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

        recorder = RecorderActor(self.root / "capture")
        self.proxy_runner = web.AppRunner(
            build_app(
                {"chatgpt": self.upstream_url, "zai-coding": self.upstream_url,
                 "codex": self.upstream_url + "/backend-api/codex",
                 "zai-coding-plan": self.upstream_url + "/api/coding/paas/v4"},
                recorder,
                classifier=None,
            )
        )
        await self.proxy_runner.setup()
        self.proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await self.proxy_site.start()
        proxy_port = self.proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self) -> None:
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def _upstream(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.upstream_requests.append(
            {
                "path": request.path,
                "method": request.method,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if request.path.endswith("/responses"):
            if not json.loads(body).get("stream"):
                return web.json_response({"id": "response-fixture", "usage": {"input_tokens": 9, "output_tokens": 2}})
            payload = (
                b'data: {"type":"response.output_text.delta","delta":"hello"}\r\n\r\n'
                b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":3}}}\r\n\r\n'
                b"data: [DONE]\r\n\r\n"
            )
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            await response.write(payload[:37])
            await response.write(payload[37:])
            await response.write_eof()
            return response

        if request.path.endswith("/chat/completions") and json.loads(body).get("stream"):
            payload = (
                b'data: {"choices":[{"delta":{"content":"zai"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n'
                b"data: [DONE]\n\n"
            )
            response = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await response.prepare(request)
            await response.write(payload[:29])
            await response.write(payload[29:])
            await response.write_eof()
            return response

        if request.path.endswith("/chat/completions"):
            return web.Response(
                status=200,
                body=b'{"id":"zai-nonstream","usage":{"prompt_tokens":13,"completion_tokens":5}}',
                content_type="application/json",
            )
        return web.Response(status=404, body=b"unexpected fixture path")

    async def _post(self, path: str, body: bytes, headers: dict[str, str]) -> bytes:
        async with ClientSession() as session:
            async with session.post(
                f"{self.proxy_url}{path}", data=body, headers=headers
            ) as response:
                self.assertEqual(response.status, 200)
                return await response.read()

    def _events(self) -> list[dict]:
        path = self.root / "capture" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_named_endpoint_prefixes_and_codex_nonstreaming(self) -> None:
        returned = await self._post("/codex/responses", b'{"model":"codex-test","stream":false}',
                                    {"Authorization": "Bearer codex-fixture-secret", "ChatGPT-Account-Id": "acct_fixture_123"})
        self.assertEqual(json.loads(returned)["usage"]["input_tokens"], 9)
        self.assertEqual(self.upstream_requests[-1]["path"], "/backend-api/codex/responses")
        await self._post("/zai-coding-plan/chat/completions", b'{"model":"glm","stream":false}', {})
        self.assertEqual(self.upstream_requests[-1]["path"], "/api/coding/paas/v4/chat/completions")
        self.assertEqual(len(self._events()), 2)

    async def test_chatgpt_codex_stream_preserves_headers_path_bytes_and_capture(self) -> None:
        response_bytes = (
            b'data: {"type":"response.output_text.delta","delta":"hello"}\r\n\r\n'
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":3}}}\r\n\r\n'
            b"data: [DONE]\r\n\r\n"
        )
        auth = "Bearer codex-fixture-secret"
        body = b'{"model":"codex-test","stream":true}'
        returned = await self._post(
            "/chatgpt/backend-api/codex/responses",
            body,
            {
                "Authorization": auth,
                "ChatGPT-Account-Id": "acct_fixture_123",
                "OpenAI-Beta": "responses=experimental",
                "originator": "codex_cli_rs",
                "Content-Type": "application/json",
            },
        )

        self.assertEqual(returned, response_bytes)
        observed = self.upstream_requests[-1]
        self.assertEqual(observed["path"], "/backend-api/codex/responses")
        for name, expected in {
            "Authorization": auth,
            "ChatGPT-Account-Id": "acct_fixture_123",
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
        }.items():
            self.assertEqual(observed["headers"][name], expected)
        self.assertEqual(observed["body"], body)

        event = self._events()[-1]
        self.assertEqual(event["provider"], "chatgpt")
        self.assertEqual(event["path"], "/backend-api/codex/responses")
        self.assertEqual(event["response_body"]["text"], response_bytes.decode())
        self.assertEqual(event["input_tokens"], 7)
        self.assertEqual(event["output_tokens"], 3)
        self.assertEqual(event["request_headers"]["Authorization"], "<redacted>")
        serialized = json.dumps(event)
        self.assertNotIn(auth, serialized)

    async def test_zai_coding_stream_and_nonstream_transport_and_usage(self) -> None:
        streaming_body = b'{"model":"glm-test","stream":true}'
        streaming_response = (
            b'data: {"choices":[{"delta":{"content":"zai"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n'
            b"data: [DONE]\n\n"
        )
        returned_stream = await self._post(
            "/zai-coding/api/coding/paas/v4/chat/completions",
            streaming_body,
            {"Authorization": "Bearer zai-fixture-secret", "Content-Type": "application/json"},
        )
        self.assertEqual(returned_stream, streaming_response)
        self.assertEqual(self.upstream_requests[-1]["path"], "/api/coding/paas/v4/chat/completions")
        self.assertEqual(self.upstream_requests[-1]["body"], streaming_body)

        nonstream_body = b'{"model":"glm-test","stream":false}'
        nonstream_response = b'{"id":"zai-nonstream","usage":{"prompt_tokens":13,"completion_tokens":5}}'
        returned_nonstream = await self._post(
            "/zai-coding/api/coding/paas/v4/chat/completions",
            nonstream_body,
            {"Authorization": "Bearer zai-fixture-secret", "Content-Type": "application/json"},
        )
        self.assertEqual(returned_nonstream, nonstream_response)
        self.assertEqual(self.upstream_requests[-1]["path"], "/api/coding/paas/v4/chat/completions")

        events = self._events()
        self.assertEqual([event["provider"] for event in events], ["zai-coding", "zai-coding"])
        self.assertEqual([event["path"] for event in events], [
            "/api/coding/paas/v4/chat/completions",
            "/api/coding/paas/v4/chat/completions",
        ])
        self.assertEqual((events[0]["input_tokens"], events[0]["output_tokens"]), (11, 4))
        self.assertEqual((events[1]["input_tokens"], events[1]["output_tokens"]), (13, 5))
        for event in events:
            self.assertEqual(event["request_headers"]["Authorization"], "<redacted>")
            self.assertNotIn("zai-fixture-secret", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
