import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class TransportErrorEvidenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        upstream = web.Application()
        upstream.router.add_post("/v1/http-error", self.http_error)
        upstream.router.add_post("/v1/router-error", self.router_error)
        upstream.router.add_post("/v1/truncated", self.truncated)
        upstream.router.add_post("/v1/reset", self.reset)
        upstream.router.add_post("/v1/slow", self.slow)
        upstream.router.add_post("/v1/ok", self.ok)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]

        self.recorder = RecorderActor(self.root)
        proxy = build_app(
            {"openrouter": f"http://127.0.0.1:{upstream_port}"},
            self.recorder,
            classifier=None,
        )
        self.proxy_runner = web.AppRunner(proxy)
        await self.proxy_runner.setup()
        proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        self.proxy_port = proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{self.proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def http_error(self, request):
        await request.read()
        return web.json_response(
            {"error": {"code": "budget_exhausted", "message": "do-not-copy-this-message"}},
            status=403,
        )

    async def router_error(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"error":{"code":504,"message":"Upstream idle timeout exceeded","metadata":{"provider_name":"fixture"}},"choices":[{"finish_reason":"error"}]}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def truncated(self, request):
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

    async def reset(self, request):
        await request.read()
        transport = request.transport
        self.assertIsNotNone(transport)
        transport.abort()
        return web.Response(status=200)

    async def slow(self, request):
        await request.read()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n')
        await asyncio.sleep(0.15)
        try:
            await response.write(b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n')
            await response.write_eof()
        except ConnectionError:
            pass
        return response

    async def ok(self, request):
        await request.read()
        return web.json_response({"choices": [{"message": {"content": "ok"}}]})

    async def _post(self, path):
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.proxy_url}/openrouter{path}",
                json={"model": "fixture/model", "messages": [{"role": "user", "content": "go"}]},
            ) as response:
                return response.status, await response.read()

    async def _errors(self):
        await self.recorder.flush()
        path = self.root / "errors.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    async def test_http_credit_failure_is_gateway_or_account_error_without_body_leak(self):
        status, _ = await self._post("/v1/http-error")
        self.assertEqual(status, 403)

        errors = await self._errors()
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["error_class"], "upstream_http_error")
        self.assertEqual(error["domain"], "transport")
        self.assertEqual(error["attribution_scope"], "gateway_or_account")
        self.assertEqual(error["response_status"], 403)
        self.assertEqual(error["error_code"], "budget_exhausted")
        self.assertIsNone(error["model"])
        self.assertNotIn("do-not-copy-this-message", json.dumps(error))

    async def test_openrouter_in_stream_error_is_router_error_even_under_http_200(self):
        status, _ = await self._post("/v1/router-error")
        self.assertEqual(status, 200)

        errors = await self._errors()
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["error_class"], "router_error")
        self.assertEqual(error["response_status"], 200)
        self.assertEqual(error["error_code"], 504)
        self.assertEqual(error["attribution_scope"], "gateway")
        self.assertNotIn("Upstream idle timeout exceeded", json.dumps(error))

    async def test_silent_sse_eof_is_stream_protocol_error(self):
        status, _ = await self._post("/v1/truncated")
        self.assertEqual(status, 200)

        errors = await self._errors()
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["error_class"], "stream_protocol_error")
        self.assertEqual(error["status_kind"], "upstream")
        self.assertEqual(error["response_status"], 200)

    async def test_pre_response_connection_reset_is_recorded_as_upstream_reset(self):
        status, _ = await self._post("/v1/reset")
        self.assertEqual(status, 502)

        errors = await self._errors()
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["error_class"], "upstream_connection_reset")
        self.assertEqual(error["response_status"], 502)
        self.assertEqual(error["status_kind"], "upstream_connect_error")
        self.assertEqual(error["model"], "fixture/model")

    async def test_downstream_disconnect_is_typed_without_creating_provider_failure_capture(self):
        body = json.dumps(
            {"model": "fixture/model", "messages": [{"role": "user", "content": "go"}]}
        ).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy_port)
        writer.write(
            b"POST /openrouter/v1/slow HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.proxy_port}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
        writer.transport.abort()
        await asyncio.sleep(0.3)

        errors = await self._errors()
        self.assertEqual(len(errors), 1)
        error = errors[0]
        self.assertEqual(error["error_class"], "downstream_client_disconnect")
        self.assertEqual(error["domain"], "transport")
        self.assertEqual(error["attribution_scope"], "client")
        self.assertEqual(error["model"], "fixture/model")

        events_path = self.root / "events.jsonl"
        events = events_path.read_text().splitlines() if events_path.exists() else []
        self.assertEqual(events, [])

    async def test_success_does_not_emit_transport_error(self):
        status, _ = await self._post("/v1/ok")
        self.assertEqual(status, 200)
        self.assertEqual(await self._errors(), [])


if __name__ == "__main__":
    unittest.main()
