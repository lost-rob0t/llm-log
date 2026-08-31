import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class FakeExpertPlane:
    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.health_calls = 0

    async def health(self) -> None:
        self.health_calls += 1
        if self.failure is not None:
            raise self.failure


class ExpertProxyPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream_hits = 0

        upstream = web.Application()
        upstream.router.add_post("/v1/messages", self._upstream)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

        self.proxy_runner = None

    async def asyncTearDown(self):
        if self.proxy_runner is not None:
            await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def _upstream(self, request):
        self.upstream_hits += 1
        await request.read()
        return web.json_response({"ok": True})

    async def _start_proxy(self, *, expert_plane, require_expert_plane):
        recorder = RecorderActor(self.root)
        app = build_app(
            {"test": self.upstream_url},
            recorder,
            classifier=None,
            expert_plane=expert_plane,
            require_expert_plane=require_expert_plane,
        )
        self.proxy_runner = web.AppRunner(app)
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def _post(self, proxy_url):
        async with ClientSession() as session:
            async with session.post(
                f"{proxy_url}/test/v1/messages",
                json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            ) as response:
                return response.status, await response.text()

    async def test_unavailable_expert_fails_open_by_default(self):
        expert = FakeExpertPlane(RuntimeError("secret expert failure detail"))
        proxy_url = await self._start_proxy(
            expert_plane=expert,
            require_expert_plane=False,
        )

        status, _ = await self._post(proxy_url)

        self.assertEqual(status, 200)
        self.assertEqual(self.upstream_hits, 1)
        self.assertEqual(expert.health_calls, 1)

    async def test_unavailable_expert_fails_closed_when_required(self):
        expert = FakeExpertPlane(RuntimeError("secret expert failure detail"))
        proxy_url = await self._start_proxy(
            expert_plane=expert,
            require_expert_plane=True,
        )

        status, body = await self._post(proxy_url)

        self.assertEqual(status, 503)
        self.assertEqual(self.upstream_hits, 0)
        self.assertEqual(expert.health_calls, 1)
        self.assertNotIn("secret expert failure detail", body)

    async def test_missing_expert_fails_closed_when_required(self):
        proxy_url = await self._start_proxy(
            expert_plane=None,
            require_expert_plane=True,
        )

        status, _ = await self._post(proxy_url)

        self.assertEqual(status, 503)
        self.assertEqual(self.upstream_hits, 0)

    async def test_healthy_expert_allows_required_forwarding(self):
        expert = FakeExpertPlane()
        proxy_url = await self._start_proxy(
            expert_plane=expert,
            require_expert_plane=True,
        )

        status, _ = await self._post(proxy_url)

        self.assertEqual(status, 200)
        self.assertEqual(self.upstream_hits, 1)
        self.assertEqual(expert.health_calls, 1)


if __name__ == "__main__":
    unittest.main()
