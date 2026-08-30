import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, WSServerHandshakeError, web

from llm_log.expert_adapter import SubprocessExpertPlane
from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


_HEALTHY_CHILD = r'''
import json
import sys

for raw in sys.stdin:
    request = json.loads(raw)
    if request.get("version") != 1 or request.get("operation") != "health":
        reply = {"status": "error", "error": {"code": "undeclared_operation"}}
    else:
        reply = {"status": "ok", "result": {"healthy": True}}
    print(json.dumps(reply, separators=(",", ":")), flush=True)
'''


class ExpertTransportLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.http_hits = 0
        self.websocket_hits = 0

        upstream = web.Application()
        upstream.router.add_post("/v1/messages", self._http_upstream)
        upstream.router.add_get("/v1/realtime", self._websocket_upstream)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

        self.proxy_runner = None
        self.expert = None

    async def asyncTearDown(self):
        if self.proxy_runner is not None:
            await self.proxy_runner.cleanup()
        if self.expert is not None:
            await self.expert.close()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def _http_upstream(self, request):
        self.http_hits += 1
        await request.read()
        return web.json_response({"ok": True})

    async def _websocket_upstream(self, request):
        self.websocket_hits += 1
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            if message.type == web.WSMsgType.TEXT:
                await socket.send_str(message.data)
        return socket

    def _healthy_expert(self):
        return SubprocessExpertPlane(
            [sys.executable, "-u", "-c", _HEALTHY_CHILD],
            data_dir=self.root / "expert",
            timeout=2.0,
            append_service_args=False,
        )

    def _unavailable_expert(self):
        return SubprocessExpertPlane(
            [str(self.root / "missing-llm-log-expert")],
            data_dir=self.root / "expert-missing",
            timeout=0.2,
            append_service_args=False,
        )

    async def _start_proxy(self, *, expert, required):
        self.expert = expert
        recorder = RecorderActor(self.root / "capture")
        app = build_app(
            {"test": self.upstream_url},
            recorder,
            classifier=None,
            expert_plane=expert,
            require_expert_plane=required,
        )
        self.proxy_runner = web.AppRunner(app)
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def test_app_owns_healthy_adapter_lifecycle_and_http_required_forwarding(self):
        expert = self._healthy_expert()
        proxy_url = await self._start_proxy(expert=expert, required=True)

        self.assertTrue(expert.running)
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/test/v1/messages",
                json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            ) as response:
                self.assertEqual(response.status, 200)
                await response.read()

        self.assertEqual(self.http_hits, 1)
        process = expert._process
        self.assertIsNotNone(process)

        await self.proxy_runner.cleanup()
        self.proxy_runner = None
        self.assertFalse(expert.running)

    async def test_unavailable_adapter_fails_open_for_websocket_by_default(self):
        proxy_url = await self._start_proxy(expert=self._unavailable_expert(), required=False)

        async with ClientSession() as session:
            async with session.ws_connect(f"{proxy_url}/test/v1/realtime") as socket:
                await socket.send_str("ping")
                reply = await socket.receive_str()
                self.assertEqual(reply, "ping")

        self.assertEqual(self.websocket_hits, 1)

    async def test_unavailable_adapter_fails_closed_for_required_websocket(self):
        proxy_url = await self._start_proxy(expert=self._unavailable_expert(), required=True)

        async with ClientSession() as session:
            with self.assertRaises(WSServerHandshakeError) as raised:
                await session.ws_connect(f"{proxy_url}/test/v1/realtime")

        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(self.websocket_hits, 0)

    async def test_healthy_required_websocket_relays_frames(self):
        expert = self._healthy_expert()
        proxy_url = await self._start_proxy(expert=expert, required=True)

        async with ClientSession() as session:
            async with session.ws_connect(f"{proxy_url}/test/v1/realtime") as socket:
                await socket.send_str("hello")
                reply = await socket.receive_str()
                self.assertEqual(reply, "hello")

        self.assertTrue(expert.running)
        self.assertEqual(self.websocket_hits, 1)


if __name__ == "__main__":
    unittest.main()
