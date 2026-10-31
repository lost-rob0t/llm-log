from __future__ import annotations

import unittest

from aiohttp import ClientSession, web

from llm_log.expert_admin import build_expert_admin_app, install_expert_admin_listener


class FakePlane:
    async def health(self):
        return {"runtime": "fixture"}

    async def query(self, operation, payload):
        return {"operation": operation, "payload": payload}

    async def observe_request(self, **_kwargs):
        return {"projection_state": "created"}

    async def classify_request(self, **_kwargs):
        return {"assertions": []}

    async def observe_usage(self, **_kwargs):
        return {"projection_state": "created"}

    async def record_outcome_evidence(self, **_kwargs):
        return {"outcome": "unknown"}


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = build_expert_admin_app(FakePlane())
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_query_allows_declared_reads(self):
        async with ClientSession() as session:
            async with session.post(
                self.base + "/query",
                json={"operation": "query_outcome_dataset", "payload": {"outcome": "success"}},
            ) as response:
                self.assertEqual(response.status, 200)
                body = await response.json()
        self.assertEqual(body["result"]["operation"], "query_outcome_dataset")

    async def test_query_rejects_mutating_operations(self):
        async with ClientSession() as session:
            async with session.post(
                self.base + "/query",
                json={"operation": "record_outcome_evidence", "payload": {}},
            ) as response:
                self.assertEqual(response.status, 400)
                body = await response.json()
        self.assertEqual(body["error"]["code"], "operation_not_read_only")


class AdminListenerPolicyTests(unittest.TestCase):
    def test_listener_must_be_loopback(self):
        app = web.Application()
        with self.assertRaises(ValueError):
            install_expert_admin_listener(app, FakePlane(), listen="0.0.0.0", port=8788)

    def test_zero_port_disables_listener(self):
        app = web.Application()
        before = len(app.on_startup)
        install_expert_admin_listener(app, FakePlane(), listen="0.0.0.0", port=0)
        self.assertEqual(len(app.on_startup), before)


if __name__ == "__main__":
    unittest.main()
