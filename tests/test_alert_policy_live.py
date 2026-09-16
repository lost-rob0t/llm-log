import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class LiveAlertPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = 0

        upstream = web.Application()
        upstream.router.add_post("/v1/flaky", self.flaky)
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
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def flaky(self, request):
        await request.read()
        self.calls += 1
        if self.calls == 1:
            return web.json_response(
                {
                    "provider": "ProviderA",
                    "openrouter_metadata": {
                        "attempts": [
                            {
                                "provider": "ProviderA",
                                "status": 502,
                                "error": {"code": "upstream_reset", "message": "secret"},
                            }
                        ]
                    },
                    "error": {"code": "upstream_reset", "message": "secret"},
                },
                status=502,
            )
        return web.json_response(
            {
                "provider": "ProviderB",
                "openrouter_metadata": {
                    "attempts": [{"provider": "ProviderB", "status": 200}]
                },
                "choices": [{"message": {"content": "recovered"}}],
            }
        )

    async def _post(self):
        payload = {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "same request"}],
        }
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.proxy_url}/openrouter/v1/flaky",
                json=payload,
            ) as response:
                return response.status, await response.read()

    async def test_failure_then_recovery_persists_alert_and_suppression(self):
        first_status, _ = await self._post()
        second_status, _ = await self._post()
        self.assertEqual((first_status, second_status), (502, 200))

        await self.recorder.flush()
        decisions_path = self.root / "alert-decisions.jsonl"
        self.assertTrue(decisions_path.exists())
        decisions = [
            json.loads(line)
            for line in decisions_path.read_text().splitlines()
            if line
        ]
        self.assertEqual(len(decisions), 2)
        first, second = decisions
        self.assertEqual(first["action"], "alert_if_unrecovered")
        self.assertEqual(first["rate_class"], "provider_transient")
        self.assertEqual(first["grace_seconds"], 10)
        self.assertEqual(second["action"], "suppress_recovered")
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(second["recovery_id"].split(":")[1], first["event_id"])

        encoded = json.dumps(decisions)
        self.assertNotIn("request_sha256", encoded)
        self.assertNotIn("ProviderA", encoded)
        self.assertNotIn("ProviderB", encoded)


if __name__ == "__main__":
    unittest.main()
