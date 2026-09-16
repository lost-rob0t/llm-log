import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class RetryRoutingCorrelationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.flaky_calls = 0
        self.metadata_headers: list[str | None] = []

        upstream = web.Application()
        upstream.router.add_post("/v1/meta", self.meta)
        upstream.router.add_post("/v1/router-error", self.router_error)
        upstream.router.add_post("/v1/flaky", self.flaky)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{upstream_port}"

        await self._start_proxy()

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def _start_proxy(self):
        self.recorder = RecorderActor(self.root)
        app = build_app(
            {"openrouter": self.upstream_url, "openai": self.upstream_url},
            self.recorder,
            classifier=None,
        )
        self.proxy_runner = web.AppRunner(app)
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{port}"

    async def _restart_proxy(self):
        await self.proxy_runner.cleanup()
        await self._start_proxy()

    async def _post(self, provider: str, path: str):
        payload = {
            "model": "fixture/model",
            "messages": [{"role": "user", "content": "same request"}],
        }
        timeout = ClientTimeout(total=3)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.proxy_url}/{provider}{path}",
                json=payload,
            ) as response:
                return response.status, await response.read()

    async def _jsonl(self, name: str):
        await self.recorder.flush()
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    async def meta(self, request):
        await request.read()
        self.metadata_headers.append(request.headers.get("X-OpenRouter-Metadata"))
        return web.json_response(
            {
                "provider": "Phala",
                "openrouter_metadata": {
                    "attempts": [
                        {
                            "provider": "ProviderA",
                            "status": 502,
                            "error": {"code": 504, "message": "do-not-persist"},
                            "endpoint": {"quantization": "fp8"},
                        },
                        {"provider": "Phala", "status": 200},
                    ]
                },
                "choices": [{"message": {"content": "ok"}}],
            }
        )

    async def router_error(self, request):
        await request.read()
        self.metadata_headers.append(request.headers.get("X-OpenRouter-Metadata"))
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        await response.write(
            b'data: {"provider":"ProviderA","openrouter_metadata":{"attempts":[{"provider":"ProviderA","status":504,"error":{"code":504,"message":"attempt-secret"}}]},"error":{"code":504,"message":"router-secret","metadata":{"provider_name":"ProviderA"}},"choices":[{"finish_reason":"error"}]}\n\n'
        )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def flaky(self, request):
        await request.read()
        self.metadata_headers.append(request.headers.get("X-OpenRouter-Metadata"))
        self.flaky_calls += 1
        if self.flaky_calls == 1:
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

    async def test_only_openrouter_opts_into_router_metadata(self):
        await self._post("openrouter", "/v1/meta")
        await self._post("openai", "/v1/meta")
        self.assertEqual(self.metadata_headers, ["enabled", None])

    async def test_routing_observation_is_safe_and_attempt_scoped(self):
        status, _ = await self._post("openrouter", "/v1/meta")
        self.assertEqual(status, 200)

        routing = await self._jsonl("routing.jsonl")
        self.assertEqual(len(routing), 1)
        observation = routing[0]
        self.assertEqual(observation["routing_id"], "routing:" + observation["event_id"])
        self.assertEqual(observation["router"], "openrouter")
        self.assertEqual(observation["selected_provider"], "Phala")
        self.assertEqual(
            observation["attempts"],
            [
                {"provider": "ProviderA", "status": 502, "error_code": 504},
                {"provider": "Phala", "status": 200},
            ],
        )
        encoded = json.dumps(observation)
        self.assertNotIn("do-not-persist", encoded)
        self.assertNotIn("quantization", encoded)

    async def test_transport_error_links_to_routing_observation(self):
        status, _ = await self._post("openrouter", "/v1/router-error")
        self.assertEqual(status, 200)

        routing = await self._jsonl("routing.jsonl")
        errors = await self._jsonl("errors.jsonl")
        self.assertEqual(len(routing), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error_class"], "router_error")
        self.assertEqual(
            errors[0]["routing_observation_id"],
            routing[0]["routing_id"],
        )
        self.assertNotIn("router-secret", json.dumps(errors[0]))
        self.assertNotIn("attempt-secret", json.dumps(routing[0]))

    async def test_byte_identical_retry_records_recovery_and_provider_change(self):
        first_status, _ = await self._post("openrouter", "/v1/flaky")
        second_status, _ = await self._post("openrouter", "/v1/flaky")
        self.assertEqual((first_status, second_status), (502, 200))

        errors = await self._jsonl("errors.jsonl")
        routing = await self._jsonl("routing.jsonl")
        recoveries = await self._jsonl("recoveries.jsonl")
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(routing), 2)
        self.assertEqual(len(recoveries), 1)

        recovery = recoveries[0]
        self.assertEqual(recovery["outcome"], "recovered")
        self.assertEqual(recovery["failed_event_id"], errors[0]["event_id"])
        self.assertEqual(recovery["retry_event_id"], routing[1]["event_id"])
        self.assertEqual(recovery["request_sha256"], errors[0]["request_sha256"])
        self.assertEqual(recovery["failed_selected_provider"], "ProviderA")
        self.assertEqual(recovery["retry_selected_provider"], "ProviderB")
        self.assertIs(recovery["provider_changed"], True)
        self.assertGreaterEqual(recovery["retry_delay_ms"], 0)

    async def test_pending_failure_survives_recorder_restart(self):
        first_status, _ = await self._post("openrouter", "/v1/flaky")
        self.assertEqual(first_status, 502)
        self.assertEqual(await self._jsonl("recoveries.jsonl"), [])

        await self._restart_proxy()
        second_status, _ = await self._post("openrouter", "/v1/flaky")
        self.assertEqual(second_status, 200)

        recoveries = await self._jsonl("recoveries.jsonl")
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0]["outcome"], "recovered")
        self.assertEqual(recoveries[0]["failed_selected_provider"], "ProviderA")
        self.assertEqual(recoveries[0]["retry_selected_provider"], "ProviderB")


if __name__ == "__main__":
    unittest.main()
